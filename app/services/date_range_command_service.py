"""반복 기간(ImportantDateRange)의 생성·수정·삭제 — 어시스턴트와 화면 버튼이 같이 쓰는 경로.

모든 변경은 변경 전 스냅샷과 함께 ActionHistory에 한 건으로 기록되어 되돌릴 수 있다. 기간이 바뀌면 그 기간을
쓰는 반복 일정의 회차를 다시 맞춘다: 늘어난 날짜의 회차를 만들고, 범위 밖으로 나간 대기(pending) 회차는
취소한다. 이미 완료·놓침인 기록은 건드리지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import local_today
from app.core.exceptions import ConflictError, InvalidInputError
from app.i18n import render_message
from app.models.action_history import ActionHistory
from app.models.enums import ActionSource, ActionType, EventInstanceStatus
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.services.action_history_service import AfterCommit, Snapshot, finish_change, record_action
from app.services.event_service import remove_event
from app.services.recurrence import generate_event_instances

DeleteMode = Literal["range_only", "with_events"]
_DATE = re.compile(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})")


def format_day(day: date) -> str:
    return f"{day.month}/{day.day}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def same_name(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


# --- 조회 --------------------------------------------------------------------------


def user_ranges(db: Session, user_id: int) -> list[ImportantDateRange]:
    return list(
        db.execute(
            select(ImportantDateRange).where(ImportantDateRange.user_id == user_id).order_by(ImportantDateRange.start_date, ImportantDateRange.id)
        ).scalars()
    )


def find_by_name(db: Session, user_id: int, name: str) -> ImportantDateRange | None:
    """이름으로 찾는다(대소문자·공백 무시). 정확히 같은 이름이 없으면 부분 일치가 하나뿐일 때만 그것."""
    ranges = user_ranges(db, user_id)
    key = _norm(name)
    exact = [r for r in ranges if _norm(r.name) == key]
    if exact:
        return exact[0]
    partial = [r for r in ranges if key and key in _norm(r.name)]
    return partial[0] if len(partial) == 1 else None


def events_using(db: Session, date_range_id: int) -> list[Event]:
    """이 기간을 반복 기준으로 쓰는 일정(하위 일정 제외)."""
    return list(
        db.execute(
            select(Event).where(Event.date_range_id == date_range_id, Event.parent_event_id.is_(None)).order_by(Event.id)
        ).scalars()
    )


def usage_counts(db: Session, user_id: int) -> dict[int, int]:
    rows = db.execute(
        select(Event.date_range_id, func.count())
        .where(Event.user_id == user_id, Event.date_range_id.is_not(None), Event.parent_event_id.is_(None))
        .group_by(Event.date_range_id)
    ).all()
    return {range_id: count for range_id, count in rows}


# --- 날짜·이름 --------------------------------------------------------------------


def parse_day(value: str, year: int) -> date:
    """'YYYY-MM-DD' 또는 연도 없는 'MM-DD'(year를 붙인다)."""
    match = _DATE.fullmatch(value.strip())
    if match is None:
        raise InvalidInputError(f"not a date: {value!r}")
    y, m, d = match.groups()
    try:
        return date(int(y) if y else year, int(m), int(d))
    except ValueError as exc:
        raise InvalidInputError(f"not a date: {value!r}") from exc


def resolve_range_dates(start: str | None, end: str, today: date, default_start: date) -> tuple[date, date]:
    """기간의 시작·종료일. 연도가 없으면 올해로 보고, 종료가 시작보다 앞이면 종료를 다음 해로 넘긴다
    ('11월 1일부터 2월 28일까지'). 시작일을 말하지 않았으면 default_start."""
    start_day = parse_day(start, today.year) if start else default_start
    end_day = parse_day(end, start_day.year)
    if end_day < start_day and not _DATE.fullmatch(end.strip()).group(1):
        end_day = end_day.replace(year=end_day.year + 1)
    if end_day < start_day:
        raise InvalidInputError("end_date must not be before start_date")
    return start_day, end_day


# --- 회차 맞추기 ------------------------------------------------------------------


@dataclass
class RangeSync:
    added: list[EventInstance] = field(default_factory=list)
    cancelled: list[EventInstance] = field(default_factory=list)
    event_ids: set[int] = field(default_factory=set)


def sync_range_instances(db: Session, date_range: ImportantDateRange, snapshot: Snapshot) -> RangeSync:
    """기간 안의 오늘 이후 날짜에 빠진 회차를 만들고, 범위 밖의 대기 회차는 취소한다 (커밋하지 않음).
    하위 일정(이동시간 등)도 부모와 같은 기간을 쓰므로 함께 맞춘다."""
    result = RangeSync()
    events = db.execute(select(Event).where(Event.date_range_id == date_range.id, Event.is_recurring.is_(True))).scalars().all()
    for event in events:
        result.event_ids.add(event.id)
        result.added.extend(generate_event_instances(db, event, not_before=local_today()))
        for instance in event.instances:
            outside = instance.date < date_range.start_date or instance.date > date_range.end_date
            if outside and instance.status == EventInstanceStatus.PENDING:
                snapshot.add_instance(instance)
                instance.status = EventInstanceStatus.CANCELLED
                result.cancelled.append(instance)
    db.flush()
    return result


# --- 기록되는 변경 ----------------------------------------------------------------


def _finish(
    db: Session,
    user: User,
    action: ActionHistory,
    event_ids: set[int],
    instance_ids: list[int],
    dates: list[date],
    defer: list[AfterCommit] | None,
) -> None:
    finish_change(db, action, AfterCommit(user.id, set(event_ids), list(instance_ids), list(dates)), defer)


def build_date_range(db: Session, user_id: int, name: str, start: date, end: date) -> ImportantDateRange:
    if end < start:
        raise InvalidInputError("end_date must not be before start_date")
    date_range = ImportantDateRange(user_id=user_id, name=name, start_date=start, end_date=end)
    db.add(date_range)
    db.flush()
    return date_range


def create_range(
    db: Session,
    user: User,
    name: str,
    start: date,
    end: date,
    source: ActionSource,
    *,
    defer: list[AfterCommit] | None = None,
) -> tuple[ImportantDateRange, ActionHistory]:
    date_range = build_date_range(db, user.id, name, start, end)
    action = record_action(
        db,
        user_id=user.id,
        action_type=ActionType.CREATE,
        source=source,
        summary_text=render_message(
            "summary.range_create", user.preferred_language, name=name, start=format_day(start), end=format_day(end)
        ),
        snapshot=None,
        affected_ids={"created_date_ranges": [date_range.id], "date_ranges": [date_range.id]},
    )
    _finish(db, user, action, set(), [], [], defer)
    if defer is None:
        db.refresh(date_range)
    return date_range, action


@dataclass
class RangeUpdateResult:
    date_range: ImportantDateRange
    action: ActionHistory
    changes: str
    sync: RangeSync


def update_range(
    db: Session,
    user: User,
    date_range: ImportantDateRange,
    *,
    name: str | None = None,
    start: date | None = None,
    end: date | None = None,
    source: ActionSource,
    defer: list[AfterCommit] | None = None,
) -> RangeUpdateResult:
    lang = user.preferred_language
    new_start, new_end = start or date_range.start_date, end or date_range.end_date
    if new_end < new_start:
        raise InvalidInputError("end_date must not be before start_date")

    snapshot = Snapshot()
    snapshot.add("important_date_ranges", date_range)
    changes: list[str] = []
    old_name, old_dates = date_range.name, (date_range.start_date, date_range.end_date)
    if name and name != date_range.name:
        changes.append(render_message("change.range_name", lang, before=old_name, after=name))
        date_range.name = name
    if (new_start, new_end) != old_dates:
        before = f"{format_day(old_dates[0])}~{format_day(old_dates[1])}"
        changes.append(render_message("change.range_dates", lang, before=before, after=f"{format_day(new_start)}~{format_day(new_end)}"))
        date_range.start_date, date_range.end_date = new_start, new_end
    db.flush()

    sync = sync_range_instances(db, date_range, snapshot)
    if sync.added or sync.cancelled:
        changes.append(render_message("change.range_instances", lang, added=len(sync.added), cancelled=len(sync.cancelled)))
    change_text = ", ".join(changes)
    action = record_action(
        db,
        user_id=user.id,
        action_type=ActionType.UPDATE,
        source=source,
        summary_text=render_message("summary.range_update", lang, name=old_name, changes=change_text),
        snapshot=snapshot,
        affected_ids={
            "date_ranges": [date_range.id],
            "events": sorted(sync.event_ids),
            "event_instances": sorted(i.id for i in sync.cancelled),
            "created_instances": sorted(i.id for i in sync.added),
        },
    )
    touched = [i.id for i in [*sync.added, *sync.cancelled]]
    _finish(db, user, action, sync.event_ids, touched, [i.date for i in [*sync.added, *sync.cancelled]], defer)
    if defer is None:
        db.refresh(date_range)
    return RangeUpdateResult(date_range, action, change_text, sync)


class RangeInUseError(ConflictError):
    """사용 중인 기간을 처리 방법 없이 지우려 할 때. 이 기간을 쓰는 일정 목록을 함께 들고 있다."""

    def __init__(self, date_range: ImportantDateRange, events: list[Event]) -> None:
        super().__init__(f"date_range {date_range.id} is used by {len(events)} events")
        self.date_range = date_range
        self.events = events


def delete_range(
    db: Session,
    user: User,
    date_range: ImportantDateRange,
    mode: DeleteMode | None,
    source: ActionSource,
    *,
    defer: list[AfterCommit] | None = None,
) -> ActionHistory:
    """사용 중인 기간은 mode가 있어야 지운다.
    range_only: 기간만 지우고 일정은 이미 만들어진 마지막 회차에서 끝난다(회차·완료 기록 그대로).
    with_events: 이 기간을 쓰는 일정(회차·하위 일정 포함)도 함께 지운다."""
    lang = user.preferred_language
    events = events_using(db, date_range.id)
    if events and mode is None:
        raise RangeInUseError(date_range, events)

    snapshot = Snapshot()
    snapshot.add("important_date_ranges", date_range)
    event_ids: set[int] = set()
    instance_ids: list[int] = []
    dates: list[date] = []
    children = db.execute(select(Event).where(Event.date_range_id == date_range.id, Event.parent_event_id.is_not(None))).scalars().all()
    if mode == "with_events":
        for event in events:
            snapshot.add_event_tree(event)
            for item in [event, *event.child_events]:
                event_ids.add(item.id)
                instance_ids.extend(i.id for i in item.instances)
                dates.extend(i.date for i in item.instances)
            remove_event(db, event)
        summary = render_message("summary.range_delete_with_events", lang, name=date_range.name, count=len(events))
    else:
        for item in [*events, *children]:
            snapshot.add("events", item)
            event_ids.add(item.id)
            item.date_range_id = None
        summary = render_message("summary.range_delete", lang, name=date_range.name)
    db.flush()

    range_id = date_range.id
    db.delete(date_range)
    action = record_action(
        db,
        user_id=user.id,
        action_type=ActionType.DELETE,
        source=source,
        summary_text=summary,
        snapshot=snapshot,
        affected_ids={"date_ranges": [range_id], "events": sorted(event_ids), "event_instances": sorted(instance_ids)},
    )
    _finish(db, user, action, event_ids, instance_ids, dates, defer)
    return action
