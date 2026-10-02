"""어시스턴트가 확정한 일정 변경의 실행과 제목 매칭 (FR-2 v4).

대상 ID는 어시스턴트의 search_events가 match_events/similar_events로 찾고, 실행은 기존 서비스의 커밋하지 않는
함수(remove_event, apply_event_update, cancel_instance, set_instance_times)를 거쳐 변경 전 스냅샷과
ActionHistory를 같은 트랜잭션에서 저장한다. 화면의 삭제(delete_*_from_ui)도 같은 실행 경로를 쓴다.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import local_today
from app.core.exceptions import ConflictError, InvalidInputError
from app.i18n import render_message
from app.models.action_history import ActionHistory
from app.models.enums import ActionSource, ActionType, EventInstanceStatus, EventType, Importance
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.location import Location
from app.models.user import User
from app.schemas.event import NewEvent
from app.schemas.event_command import CommandTarget, NewDateRangeDraft, NewLocationDraft
from app.services import date_range_command_service as ranges
from app.services.action_history_service import AfterCommit, Snapshot, finish_change, record_action
from app.services.common import name_key
from app.services.event_instance_service import cancel_instance, child_instances_on_same_date, set_instance_times
from app.services.event_service import apply_event_update, build_event, remove_event, set_event_location

CommandIntent = Literal["delete", "update"]
_CANCELLED = EventInstanceStatus.CANCELLED


# --- 대상 설명 ------------------------------------------------------------------------


@dataclass
class CommandDescription:
    intent: CommandIntent
    title: str | None = None
    date: date | None = None  # 회차 하나를 가리킬 때의 날짜 (요약 문구용)
    scope: Literal["instance", "series"] | None = None
    new_start_time: str | None = None  # HH:MM
    new_end_time: str | None = None
    new_title: str | None = None
    new_importance: int | None = None
    # 단발 일정의 날짜 옮기기 (어시스턴트만 쓴다). 시각은 그대로 두고 날짜만 바꾼다.
    new_date: date | None = None
    # 장소는 반복 시리즈 단위 값이다. set인데 등록되지 않은 장소면 이동 시간(분)을 물어 새로 등록한다.
    location_action: Literal["set", "remove"] | None = None
    new_location_name: str | None = None
    new_location_minutes: int | None = None
    # 종류(일반/마감) 바꾸기는 반복 일정의 한 회차를 떼어낼 때만 쓴다.
    new_event_type: Literal["scheduled", "deadline"] | None = None

    def has_changes(self) -> bool:
        return any(
            v is not None
            for v in (
                self.new_start_time, self.new_end_time, self.new_title, self.new_importance, self.location_action, self.new_date,
                self.new_event_type,
            )
        )

    def detaches_instance(self) -> bool:
        """한 회차에 시간 말고 다른 값(제목·중요도·종류·장소)을 바꾸는지. 회차별 필드가 없어 그 회차를 시리즈에서 떼어낸다."""
        return bool(self.new_title) or self.new_importance is not None or self.new_event_type is not None or self.location_action is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat() if self.date else None
        data["new_date"] = self.new_date.isoformat() if self.new_date else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandDescription:
        # 저장된 제안(pending_proposals.payload)에 예전 필드(all, target_weekday)가 남아 있어도 읽을 수 있게 모르는 키는 버린다.
        known = {f.name for f in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["date"] = date.fromisoformat(values["date"]) if values.get("date") else None
        values["new_date"] = date.fromisoformat(values["new_date"]) if values.get("new_date") else None
        return cls(**values)

@dataclass
class Target:
    event: Event
    instance: EventInstance | None = None  # None이면 반복 전체(시리즈) 또는 단발성 이벤트 자체


# --- 대상 찾기 --------------------------------------------------------------------------



def _loose(text: str) -> str:
    """대소문자·공백·기호를 무시한 비교용 문자열."""
    return re.sub(r"[\W_]+", "", text).casefold()


def _codes(text: str) -> set[str]:
    """과목 코드처럼 제목을 구분하는 단어('ECE360', '물리')."""
    return {_loose(word) for word in re.split(r"[\s/·,()]+", text) if len(_loose(word)) >= 2}


def _active_instances(event: Event) -> list[EventInstance]:
    return [i for i in event.instances if i.status != _CANCELLED]


def _is_recurring(event: Event) -> bool:
    return event.is_recurring or len(_active_instances(event)) > 1


def match_events(db: Session, user_id: int, title: str) -> list[Event]:
    """제목 매칭: 대소문자·공백을 무시한 부분일치. 정확히 같은 제목이 있으면 그것만 (예: '물리 퀴즈'가
    '물리 퀴즈 보충'까지 잡지 않게). 하위 이동시간·준비 일정은 부모를 따라 움직이므로 대상에서 뺀다."""
    events = db.execute(
        select(Event).where(Event.user_id == user_id, Event.parent_event_id.is_(None)).order_by(Event.id)
    ).scalars().all()
    key = _loose(title)
    exact = [e for e in events if _loose(e.title) == key]
    if exact:
        return exact
    partial = [e for e in events if key and (key in _loose(e.title) or _loose(e.title) in key)]
    if partial:
        return partial
    # 단어 일부만 맞아도 후보 ('ECE360'만 말해도 'ECE360 Lab'). 숫자가 든 과목 코드가 있으면 그것만 본다 — 'Lecture'처럼
    # 흔한 단어가 모든 강의를 끌어오지 않게. 오타일 수 있으니 아주 비슷한 제목도 함께 후보로 낸다(여러 개면 고르게).
    words = _codes(title)
    codes = {word for word in words if any(ch.isdigit() for ch in word)} or words
    by_word = [e for e in events if codes & _codes(e.title)]
    if not by_word:
        return []
    similar = [e for e in similar_events(db, user_id, title, cutoff=0.88) if e not in by_word]
    return by_word + similar


def same_title_events(db: Session, user_id: int, title: str) -> list[Event]:
    """새로 만들려는 일정과 사실상 같은 제목의 일정: 대소문자·공백·기호를 무시해 같거나, 한쪽이 다른 쪽을 포함하거나(짧은 쪽이
    4자 이상), 아주 비슷한 제목. match_events와 달리 과목 코드만 같은 일정('ECE360 Lab' vs 'ECE360 Quiz')은 넣지 않는다."""
    key = _loose(title)
    if not key:
        return []
    found = []
    for event in db.execute(select(Event).where(Event.user_id == user_id, Event.parent_event_id.is_(None)).order_by(Event.id)).scalars():
        other = _loose(event.title)
        shorter = min(len(key), len(other))
        contains = shorter >= 4 and (key in other or other in key)
        if key == other or contains or difflib.SequenceMatcher(None, key, other).ratio() >= 0.85:
            found.append(event)
    return found


def similar_events(db: Session, user_id: int, title: str, limit: int = 3, cutoff: float = 0.6) -> list[Event]:
    """하나도 맞지 않을 때 보여줄 비슷한 제목 (오타: 'ECE360 Lecture' → 'ESC360 Lecture')."""
    events = db.execute(
        select(Event).where(Event.user_id == user_id, Event.parent_event_id.is_(None)).order_by(Event.id)
    ).scalars().all()
    by_key = {}
    for event in events:
        by_key.setdefault(_loose(event.title), event)
    close = difflib.get_close_matches(_loose(title), list(by_key), n=limit, cutoff=cutoff)
    return [by_key[key] for key in close]


def _format_date(day: date) -> str:
    return f"{day.month}/{day.day}"


def to_command_target(target: Target) -> CommandTarget:
    event = target.event
    return CommandTarget(
        event_id=event.id,
        event_instance_id=target.instance.id if target.instance else None,
        title=event.title,
        date=target.instance.date if target.instance else event.anchor_time.date(),
        is_recurring=_is_recurring(event),
    )


# --- 수정 값 계산 -------------------------------------------------------------------------


def _hhmm(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise InvalidInputError(f"time must be HH:MM, got {value!r}") from exc


def new_times(desc: CommandDescription, day: date, start: datetime | None, end: datetime) -> tuple[datetime | None, datetime]:
    """시작만 바꾸면 기존 지속 시간을 유지해 종료도 민다 (5–6시 → 6–7시). 종료를 말하면 그 값을 쓴다.
    deadline(start=None)은 마감 시각만 바꾼다. new_date가 있으면 결과를 그 날짜로 옮긴다."""
    shift = (desc.new_date - day) if desc.new_date is not None else timedelta(0)
    if start is None:
        new_deadline = desc.new_end_time or desc.new_start_time
        return None, (datetime.combine(day, _hhmm(new_deadline)) if new_deadline else end) + shift
    new_start = datetime.combine(day, _hhmm(desc.new_start_time)) if desc.new_start_time else start
    new_end = datetime.combine(day, _hhmm(desc.new_end_time)) if desc.new_end_time else new_start + (end - start)
    return new_start + shift, new_end + shift


def _time_range(start: datetime | None, end: datetime) -> str:
    return end.strftime("%H:%M") if start is None else f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _describe_changes(lang: str, before: tuple[datetime | None, datetime], after: tuple[datetime | None, datetime], event: Event, desc: CommandDescription) -> str:
    changes = []
    if before != after:
        key = "change.deadline" if before[0] is None else "change.time"
        changes.append(render_message(key, lang, before=_time_range(*before), after=_time_range(*after)))
    if desc.new_title and desc.new_title != event.title:
        changes.append(render_message("change.title", lang, before=event.title, after=desc.new_title))
    if desc.new_importance is not None and event.importance != Importance(desc.new_importance):
        before_importance = event.importance.value if event.importance is not None else "-"
        changes.append(render_message("change.importance", lang, before=before_importance, after=desc.new_importance))
    if desc.new_event_type is not None and desc.new_event_type != event.event_type.value:
        changes.append(
            render_message(
                "change.event_type",
                lang,
                before=render_message(f"event_type.{event.event_type.value}", lang),  # type: ignore[arg-type]
                after=render_message(f"event_type.{desc.new_event_type}", lang),  # type: ignore[arg-type]
            )
        )
    return ", ".join(changes) or "-"


def _series_field_changes(desc: CommandDescription) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if desc.new_title:
        changes["title"] = desc.new_title
    if desc.new_importance is not None:
        changes["importance"] = Importance(desc.new_importance)
    return changes


# --- 실행 -----------------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    action: ActionHistory
    affected: list[CommandTarget]
    message: str


def execute(
    db: Session,
    user: User,
    desc: CommandDescription,
    targets: list[Target],
    source: ActionSource,
    *,
    defer: list[AfterCommit] | None = None,
) -> ExecutionResult:
    """변경 전 스냅샷 → 변경 적용 → ActionHistory 기록을 한 트랜잭션으로 커밋하고, 지난 날짜가 영향을 받았으면
    포인트 원장을 다시 계산한다. defer를 주면 커밋하지 않는다(finish_change)."""
    lang = user.preferred_language
    snapshot = Snapshot()
    event_ids: set[int] = set()
    instance_ids: set[int] = set()
    touched_dates: list[date] = []
    summaries: list[str] = []
    affected = [to_command_target(t) for t in targets]
    created: dict[str, list[int]] = {"created_children": [], "created_instances": [], "created_locations": [], "created_events": []}

    for target in targets:
        event, instance = target.event, target.instance
        event_ids.add(event.id)

        if desc.intent == "delete" and instance is None:
            snapshot.add_event_tree(event)
            for item in [event, *event.child_events]:
                event_ids.add(item.id)
                instance_ids.update(i.id for i in item.instances)
                touched_dates.extend(i.date for i in item.instances)
            summaries.append(render_message("summary.delete_series", lang, title=event.title))
            remove_event(db, event)

        elif desc.intent == "delete":
            for item in [instance, *child_instances_on_same_date(db, instance)]:
                snapshot.add_instance(item)
            for item in cancel_instance(db, instance):
                instance_ids.add(item.id)
                event_ids.add(item.event_id)
            touched_dates.append(instance.date)
            summaries.append(render_message("summary.delete_instance", lang, title=event.title, date=_format_date(instance.date)))

        elif instance is None:  # 반복 전체(또는 단발성 이벤트) 수정
            snapshot.add_event_with_children(event)
            event_ids.update(child.id for child in event.child_events)
            location_text = None
            if desc.location_action is not None:
                location_text = _change_location(db, user, event, desc, snapshot, created, event_ids, instance_ids)
            before = (event.start_time, event.end_time)
            anchor_day = (event.start_time or event.end_time).date()
            new_start, new_end = new_times(desc, anchor_day, event.start_time, event.end_time)
            change_text = _describe_changes(lang, before, (new_start, new_end), event, desc)
            if location_text:
                change_text = location_text if change_text == "-" else f"{change_text}, {location_text}"
            changes = _series_field_changes(desc)
            if (new_start, new_end) != before:
                changes.update({"start_time": new_start, "end_time": new_end})
            title_before = event.title
            apply_event_update(db, event, changes)
            summaries.append(render_message("summary.update_series", lang, title=title_before, changes=change_text))

        elif desc.detaches_instance():  # 한 회차만 제목·중요도·종류·장소 변경 → 그 회차를 떼어 단발 일정으로
            change_text = _detach_instance(db, user, event, instance, desc, snapshot, created, event_ids, instance_ids)
            touched_dates.append(instance.date)
            summaries.append(
                render_message("summary.detach_instance", lang, title=event.title, date=_format_date(instance.date), changes=change_text)
            )

        else:  # 한 회차만 시간 변경 → 회차 시간 override
            for item in [instance, *child_instances_on_same_date(db, instance)]:
                snapshot.add_instance(item)
            before = (instance.effective_start, instance.effective_end)
            after = new_times(desc, instance.date, *before)
            change_text = _describe_changes(lang, before, after, event, desc)
            if after != before:
                for item in set_instance_times(db, instance, *after):
                    instance_ids.add(item.id)
                    event_ids.add(item.event_id)
            touched_dates.append(instance.date)
            summaries.append(
                render_message("summary.update_instance", lang, title=event.title, date=_format_date(instance.date), changes=change_text)
            )

    if len(summaries) == 1:
        summary = summaries[0]
    else:
        summary = render_message(
            "summary.multiple",
            lang,
            count=len(summaries),
            action=render_message(f"summary.action.{desc.intent}", lang),  # type: ignore[arg-type]
            items=", ".join(summaries),
        )

    action = record_action(
        db,
        user_id=user.id,
        action_type=ActionType.DELETE if desc.intent == "delete" else ActionType.UPDATE,
        source=source,
        summary_text=summary,
        snapshot=snapshot,
        affected_ids={"events": sorted(event_ids), "event_instances": sorted(instance_ids), **created},
    )
    finish_change(db, action, AfterCommit(user.id, set(event_ids), sorted(instance_ids), touched_dates), defer)

    message = render_message("command.executed", lang, summary=summary)
    return ExecutionResult(action=action, affected=affected, message=message)


def detached_times(desc: CommandDescription, instance: EventInstance) -> tuple[datetime | None, datetime]:
    """떼어낸 회차의 새 시각. 종류를 바꾸면 마감 ↔ 일반에 맞게 시작을 비우거나 채운다."""
    event = instance.event
    start, end = new_times(desc, instance.date, instance.effective_start, instance.effective_end)
    new_type = desc.new_event_type or event.event_type.value
    if new_type == "deadline":
        # 일반 → 마감: 마감 시각을 말하지 않았으면 원래 끝나는 시각
        return None, datetime.combine(instance.date, _hhmm(desc.new_end_time)) if desc.new_end_time else end
    if start is None:
        # 마감 → 일반: 시작 시각이 필요하다 (끝을 말하지 않으면 1시간)
        if not desc.new_start_time:
            raise InvalidInputError("changing a deadline into a scheduled event needs start_time")
        start = datetime.combine(instance.date, _hhmm(desc.new_start_time))
        end = datetime.combine(instance.date, _hhmm(desc.new_end_time)) if desc.new_end_time else start + timedelta(hours=1)
    return start, end


def _detach_instance(
    db: Session,
    user: User,
    event: Event,
    instance: EventInstance,
    desc: CommandDescription,
    snapshot: Snapshot,
    created: dict[str, list[int]],
    event_ids: set[int],
    instance_ids: set[int],
) -> str:
    """반복 일정의 한 회차만 제목·중요도·종류·장소를 바꾼다. 회차별로 저장할 곳이 없으므로 그 회차를 cancelled로 두고
    (반복 생성이 그 날짜를 다시 만들지 않게) 같은 날짜·시각에 바뀐 값의 단발 일정을 새로 만든다. 바꾸지 않은 값은
    시리즈 값을 그대로 쓴다. 되돌리면 새 일정을 지우고 회차를 스냅샷으로 복구한다."""
    lang = user.preferred_language
    for item in [instance, *child_instances_on_same_date(db, instance)]:
        snapshot.add_instance(item)
    before = (instance.effective_start, instance.effective_end)
    start, end = detached_times(desc, instance)

    location_id = event.location_id
    location_text = None
    if desc.location_action == "remove":
        location_id = None
    elif desc.location_action == "set" and desc.new_location_name:
        location = find_location(db, user.id, desc.new_location_name)
        if location is None:
            minutes = desc.new_location_minutes if desc.new_location_minutes is not None else 0
            location = Location(user_id=user.id, name=desc.new_location_name, default_travel_minutes=minutes)
            db.add(location)
            db.flush()
            created["created_locations"].append(location.id)
        location_id = location.id
    if location_id != event.location_id:
        no_location = render_message("change.no_location", lang)
        new_location = db.get(Location, location_id) if location_id is not None else None
        location_text = render_message(
            "change.location",
            lang,
            before=event.location.name if event.location else no_location,
            after=new_location.name if new_location else no_location,
        )

    for item in cancel_instance(db, instance):
        instance_ids.add(item.id)
        event_ids.add(item.event_id)

    detached = build_event(
        db,
        NewEvent(
            user_id=user.id,
            title=desc.new_title or event.title,
            event_type=EventType(desc.new_event_type) if desc.new_event_type else event.event_type,
            start_time=start,
            end_time=end,
            importance=Importance(desc.new_importance) if desc.new_importance is not None else event.importance,
            is_recurring=False,
            location_id=location_id,
        ),
    )
    created["created_events"].append(detached.id)
    for item in [detached, *detached.child_events]:
        event_ids.add(item.id)
        instance_ids.update(i.id for i in item.instances)

    change_text = _describe_changes(lang, before, (start, end), event, desc)
    if location_text:
        change_text = location_text if change_text == "-" else f"{change_text}, {location_text}"
    return change_text


def _change_location(
    db: Session,
    user: User,
    event: Event,
    desc: CommandDescription,
    snapshot: Snapshot,
    created: dict[str, list[int]],
    event_ids: set[int],
    instance_ids: set[int],
) -> str:
    """장소를 넣거나 바꾸거나 뺀다. 되돌릴 수 있게 이동 child와 그 회차의 이전 상태를 스냅샷에 남긴다."""
    lang = user.preferred_language
    for child in event.child_events:
        for instance in child.instances:
            snapshot.add_instance(instance)
    before = event.location.name if event.location else render_message("change.no_location", lang)
    location = None
    if desc.location_action == "set" and desc.new_location_name:
        location = find_location(db, user.id, desc.new_location_name)
        if location is None:
            location = Location(user_id=user.id, name=desc.new_location_name, default_travel_minutes=desc.new_location_minutes or 0)
            db.add(location)
            db.flush()
            created["created_locations"].append(location.id)
    change = set_event_location(db, event, location)
    if change.created_child is not None:
        created["created_children"].append(change.created_child.id)
        event_ids.add(change.created_child.id)
    created["created_instances"].extend(i.id for i in change.created_instances)
    instance_ids.update(i.id for i in [*change.changed_instances, *change.created_instances])
    after = location.name if location else render_message("change.no_location", lang)
    return render_message("change.location", lang, before=before, after=after)


def find_location(db: Session, user_id: int, name: str) -> Location | None:
    """이름으로 장소를 찾는다(괄호 안 설명·대소문자·공백 무시 — "Bahen Centre (이동 10분)"도 "Bahen Centre")."""
    key = name_key(name)
    return next((loc for loc in db.execute(select(Location).where(Location.user_id == user_id)).scalars() if name_key(loc.name) == key), None)


def delete_event_from_ui(db: Session, event: Event) -> ActionHistory:
    """목록의 삭제 버튼(DELETE /events/{id}). 되돌릴 수 있게 source=ui로 기록한다."""
    user = db.get(User, event.user_id)
    return execute(db, user, CommandDescription(intent="delete", title=event.title), [Target(event)], ActionSource.UI).action


def delete_instance_from_ui(db: Session, instance: EventInstance) -> ActionHistory:
    """캘린더에서 반복 일정의 한 회차만 삭제(취소)한다. 자연어 "이번만 삭제"와 같은 경로라 되돌리기 기록이 남는다."""
    if instance.status == _CANCELLED:
        raise ConflictError(f"event instance {instance.id} is already cancelled")
    event = instance.event
    user = db.get(User, event.user_id)
    desc = CommandDescription(intent="delete", title=event.title, date=instance.date, scope="instance")
    return execute(db, user, desc, [Target(event, instance)], ActionSource.UI).action


def create_event_from_nl(
    db: Session,
    user: User,
    data: NewEvent,
    new_date_range: NewDateRangeDraft | None = None,
    new_location: NewLocationDraft | None = None,
    *,
    defer: list[AfterCommit] | None = None,
) -> ExecutionResult:
    """어시스턴트가 제안한 일정 초안을 확정한다. 초안에 새 반복 기간이 있으면 이벤트와 같은 트랜잭션에서 만든다(같은 이름의
    기간이 이미 있으면 그것을 쓴다). 되돌리기는 만든 이벤트(와 하위 일정)를 지우고, 같이 만든 기간도 다른 일정이
    쓰지 않으면 지운다."""
    lang = user.preferred_language
    created_range = None
    if new_date_range is not None:
        date_range = ranges.find_by_name(db, user.id, new_date_range.name)
        if date_range is None or not ranges.same_name(date_range.name, new_date_range.name):
            date_range = ranges.build_date_range(db, user.id, new_date_range.name, new_date_range.start_date, new_date_range.end_date)
            created_range = date_range
        data = data.model_copy(update={"date_range_id": date_range.id})
    created_location = None
    if new_location is not None:
        location = find_location(db, user.id, new_location.name)
        if location is None:
            location = Location(user_id=user.id, name=new_location.name, default_travel_minutes=new_location.default_travel_minutes)
            db.add(location)
            db.flush()
            created_location = location
        data = data.model_copy(update={"location_id": location.id})

    event = build_event(db, data, instances_from=local_today())
    if created_range is not None:
        summary = render_message("summary.create_with_range", lang, title=event.title, name=created_range.name)
    else:
        summary = render_message("summary.create", lang, title=event.title)
    all_events = [event, *event.child_events]
    action = record_action(
        db,
        user_id=user.id,
        action_type=ActionType.CREATE,
        source=ActionSource.NL,
        summary_text=summary,
        snapshot=None,
        affected_ids={
            "created_events": [event.id],
            "events": sorted(e.id for e in all_events),
            "event_instances": sorted(i.id for e in all_events for i in e.instances),
            "created_date_ranges": [created_range.id] if created_range is not None else [],
            "created_locations": [created_location.id] if created_location is not None else [],
            "date_ranges": [event.date_range_id] if event.date_range_id is not None else [],
        },
    )
    finish_change(db, action, AfterCommit(user.id, {event.id}), defer)
    if defer is None:
        db.refresh(event)
    message = render_message("command.executed", lang, summary=summary)
    return ExecutionResult(action=action, affected=[to_command_target(Target(event))], message=message)
