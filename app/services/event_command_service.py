"""FR-2 v3.6 자연어 일정 관리: 삭제·수정 대상 찾기와 실행.

LLM은 대상에 대한 "설명"(제목·날짜·전부 여부·범위)만 낸다. 어떤 Event/EventInstance인지는 여기서 DB를
보고 결정적으로 찾는다 — LLM이 ID를 고르면 환각으로 엉뚱한 일정을 지울 수 있기 때문이다.

실행은 기존 서비스의 커밋하지 않는 함수(remove_event, apply_event_update, cancel_instance,
set_instance_times)를 거치고, 변경 전 스냅샷과 ActionHistory를 같은 트랜잭션에서 저장한다.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.i18n import render_message
from app.models.action_history import ActionHistory
from app.models.enums import ActionSource, ActionType, EventInstanceStatus, Importance
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.user import User
from app.schemas.event import EventCreate
from app.schemas.event_command import CommandResult, CommandTarget
from app.services.action_history_service import Snapshot, record_action, recalculate_points_for_dates
from app.services.event_instance_service import cancel_instance, child_instances_on_same_date, set_instance_times
from app.services.event_service import apply_event_update, build_event, remove_event
from app.services.llm_client import EventSlotFillResult, LLMResponseParsingError
from app.services.notification import sync_notifications
from app.services.slot_fill_session import PendingAction, create_pending_action

CommandIntent = Literal["delete", "update"]
_CANCELLED = EventInstanceStatus.CANCELLED


# --- 대상 설명 ------------------------------------------------------------------------


@dataclass
class CommandDescription:
    intent: CommandIntent
    title: str | None = None
    date: date | None = None
    all: bool = False
    scope: Literal["instance", "series"] | None = None
    new_start_time: str | None = None  # HH:MM
    new_end_time: str | None = None
    new_title: str | None = None
    new_importance: int | None = None

    @classmethod
    def from_llm(cls, result: EventSlotFillResult) -> CommandDescription:
        return cls(
            intent=result.intent,  # type: ignore[arg-type]
            title=result.target_title,
            date=result.target_date,
            all=result.target_all,
            scope=result.target_scope,
            new_start_time=result.new_start_time,
            new_end_time=result.new_end_time,
            new_title=result.new_title,
            new_importance=int(result.new_importance) if result.new_importance is not None else None,
        )

    def merged(self, newer: CommandDescription) -> CommandDescription:
        """되묻기에 대한 답을 반영한다: 새로 채워진 값만 덮어쓴다 ("전부"는 한 번 말하면 유지)."""
        values = asdict(self)
        for key, value in asdict(newer).items():
            if key == "intent":
                continue
            if key == "all":
                values["all"] = self.all or newer.all
            elif value is not None:
                values[key] = value
        return CommandDescription(**values)

    def has_changes(self) -> bool:
        return any(v is not None for v in (self.new_start_time, self.new_end_time, self.new_title, self.new_importance))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat() if self.date else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandDescription:
        values = dict(data)
        values["date"] = date.fromisoformat(values["date"]) if values.get("date") else None
        return cls(**values)

    def describe_for_prompt(self) -> str:
        """다음 턴 LLM 프롬프트의 '진행 중인 요청'."""
        parts = [f"intent={self.intent}"]
        for key in ("title", "date", "scope", "new_start_time", "new_end_time", "new_title", "new_importance"):
            value = getattr(self, key)
            if value is not None:
                parts.append(f"{key}={value}")
        if self.all:
            parts.append("all=true")
        return ", ".join(parts)


@dataclass
class Target:
    event: Event
    instance: EventInstance | None = None  # None이면 반복 전체(시리즈) 또는 단발성 이벤트 자체


@dataclass
class Resolution:
    status: Literal["ready", "not_found", "needs_clarification"]
    message: str = ""
    targets: list[Target] = field(default_factory=list)
    candidates: list[Target] = field(default_factory=list)


# --- 대상 찾기 --------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


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
    key = _norm(title)
    exact = [e for e in events if _norm(e.title) == key]
    return exact or [e for e in events if key in _norm(e.title)]


def _label(target: Target, index: int | None = None) -> str:
    event = target.event
    if target.instance is not None:
        when = _format_date(target.instance.date)
    else:
        # 반복 일정은 시작일 뒤에 "~"를 붙여 단발성과 구분한다 (언어와 무관한 표기).
        when = _format_date(event.anchor_time.date()) + ("~" if _is_recurring(event) else "")
    prefix = f"{index}) " if index is not None else ""
    return f"{prefix}{event.title} ({when})"


def _format_date(day: date) -> str:
    return f"{day.month}/{day.day}"


def resolve(db: Session, user: User, desc: CommandDescription) -> Resolution:
    lang = user.preferred_language
    if not desc.title:
        return Resolution("needs_clarification", render_message("command.need_title", lang))
    if desc.intent == "update" and not desc.has_changes():
        return Resolution("needs_clarification", render_message("command.nothing_to_update", lang))

    events = match_events(db, user.id, desc.title)
    if not events:
        return Resolution("not_found", render_message("command.not_found", lang, title=desc.title))

    if desc.date is not None:
        targets: list[Target] = []
        for event in events:
            on_date = [i for i in _active_instances(event) if i.date == desc.date]
            if on_date and not _is_recurring(event):
                targets.append(Target(event))  # 단발 일정은 회차가 하나뿐이라 이벤트 자체를 바꾸고, 회차가 따라간다
            elif on_date:
                targets.extend(Target(event, instance) for instance in on_date)
            elif not event.instances and event.anchor_time.date() == desc.date:
                targets.append(Target(event))  # 회차가 없는 단발성 이벤트
        if not targets:
            return Resolution(
                "not_found", render_message("command.not_found_on_date", lang, title=desc.title, date=_format_date(desc.date))
            )
        if desc.scope == "series":
            unique = {t.event.id: t.event for t in targets}
            targets = [Target(event) for event in unique.values()]
        if len({t.event.id for t in targets}) > 1 and not desc.all:
            return _ambiguous(targets, lang)
        return Resolution("ready", targets=targets)

    if len(events) > 1 and not desc.all:
        return _ambiguous([Target(event) for event in events], lang)

    if len(events) == 1 and not desc.all and _is_recurring(events[0]):
        if desc.scope is None:
            return Resolution("needs_clarification", render_message("command.ask_scope", lang, title=events[0].title))
        if desc.scope == "instance":
            return Resolution("needs_clarification", render_message("command.ask_date", lang, title=events[0].title))
    return Resolution("ready", targets=[Target(event) for event in events])


def _ambiguous(candidates: list[Target], lang: str) -> Resolution:
    labels = ", ".join(_label(t, i) for i, t in enumerate(candidates, start=1))
    return Resolution("needs_clarification", render_message("command.ambiguous", lang, candidates=labels), candidates=candidates)


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
        raise LLMResponseParsingError(f"LLM이 채운 시각이 HH:MM 형식이 아닙니다: {value!r}") from exc


def _new_times(desc: CommandDescription, day: date, start: datetime | None, end: datetime) -> tuple[datetime | None, datetime]:
    """시작만 바꾸면 기존 지속 시간을 유지해 종료도 민다 (5–6시 → 6–7시). 종료를 말하면 그 값을 쓴다.
    deadline(start=None)은 마감 시각만 바꾼다."""
    if start is None:
        new_deadline = desc.new_end_time or desc.new_start_time
        return None, datetime.combine(day, _hhmm(new_deadline)) if new_deadline else end
    new_start = datetime.combine(day, _hhmm(desc.new_start_time)) if desc.new_start_time else start
    new_end = datetime.combine(day, _hhmm(desc.new_end_time)) if desc.new_end_time else new_start + (end - start)
    return new_start, new_end


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


def execute(db: Session, user: User, desc: CommandDescription, targets: list[Target], source: ActionSource) -> ExecutionResult:
    """변경 전 스냅샷 → 변경 적용 → ActionHistory 기록을 한 트랜잭션으로 커밋하고, 지난 날짜가 영향을 받았으면
    포인트 원장을 다시 계산한다."""
    lang = user.preferred_language
    snapshot = Snapshot()
    event_ids: set[int] = set()
    instance_ids: set[int] = set()
    touched_dates: list[date] = []
    summaries: list[str] = []
    notes: set[str] = set()
    affected = [to_command_target(t) for t in targets]

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
            before = (event.start_time, event.end_time)
            anchor_day = (event.start_time or event.end_time).date()
            new_start, new_end = _new_times(desc, anchor_day, event.start_time, event.end_time)
            change_text = _describe_changes(lang, before, (new_start, new_end), event, desc)
            changes = _series_field_changes(desc)
            if (new_start, new_end) != before:
                changes.update({"start_time": new_start, "end_time": new_end})
            title_before = event.title
            apply_event_update(db, event, changes)
            summaries.append(render_message("summary.update_series", lang, title=title_before, changes=change_text))

        else:  # 한 회차만 수정
            for item in [instance, *child_instances_on_same_date(db, instance)]:
                snapshot.add_instance(item)
            before = (instance.effective_start, instance.effective_end)
            after = _new_times(desc, instance.date, *before)
            change_text = _describe_changes(lang, before, after, event, desc)
            if after != before:
                for item in set_instance_times(db, instance, *after):
                    instance_ids.add(item.id)
                    event_ids.add(item.event_id)
            field_changes = _series_field_changes(desc)
            if field_changes:  # 제목·중요도는 회차별 필드가 없어 반복 전체에 적용한다
                snapshot.add_event_with_children(event)
                apply_event_update(db, event, field_changes)
                notes.add(render_message("command.title_applies_to_series", lang))
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
        affected_ids={"events": sorted(event_ids), "event_instances": sorted(instance_ids)},
    )
    db.commit()
    db.refresh(action)
    sync_notifications(db, event_ids=event_ids, instance_ids=instance_ids)
    recalculate_points_for_dates(db, user.id, touched_dates)

    message = " ".join([render_message("command.executed", lang, summary=summary), *sorted(notes)])
    return ExecutionResult(action=action, affected=affected, message=message)


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


def create_event_from_nl(db: Session, user: User, data: EventCreate) -> ExecutionResult:
    """자연어로 만든 초안을 확정한다. 되돌리기는 만든 이벤트(와 하위 일정)를 지운다."""
    event = build_event(db, data)
    summary = render_message("summary.create", user.preferred_language, title=event.title)
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
        },
    )
    db.commit()
    db.refresh(action)
    db.refresh(event)
    sync_notifications(db, event_ids=[event.id])
    message = render_message("command.executed", user.preferred_language, summary=summary)
    return ExecutionResult(action=action, affected=[to_command_target(Target(event))], message=message)


# --- 확인 대기 --------------------------------------------------------------------------


def request_confirmation(user: User, desc: CommandDescription, targets: list[Target]) -> tuple[PendingAction, str]:
    pending = create_pending_action(
        user.id,
        desc.intent,
        {
            "description": desc.to_dict(),
            "targets": [[t.event.id, t.instance.id if t.instance else None] for t in targets],
        },
    )
    labels = ", ".join(_label(t) for t in targets)
    message = render_message("command.confirm", user.preferred_language, count=len(targets), targets=labels)
    return pending, message


def execute_pending(db: Session, user: User, pending: PendingAction) -> ExecutionResult:
    if pending.kind == "create":
        return create_event_from_nl(db, user, EventCreate(**pending.payload["draft"]))

    desc = CommandDescription.from_dict(pending.payload["description"])
    targets: list[Target] = []
    for event_id, instance_id in pending.payload["targets"]:
        # 확인을 기다리는 사이 대상이 지워지거나 취소됐으면 실행하지 않는다.
        event = db.get(Event, event_id)
        instance = db.get(EventInstance, instance_id) if instance_id is not None else None
        if event is None or event.user_id != user.id or (instance_id is not None and (instance is None or instance.status == _CANCELLED)):
            raise ConflictError("the target events changed after confirmation was requested — please make the request again")
        targets.append(Target(event, instance))
    return execute(db, user, desc, targets, ActionSource.NL)


def command_result(
    action: Literal["create", "delete", "update"],
    status: Literal["executed", "needs_confirmation", "needs_clarification", "not_found"],
    *,
    targets: list[Target] | None = None,
    affected: list[CommandTarget] | None = None,
    candidates: list[Target] | None = None,
    pending: PendingAction | None = None,
    action_id: int | None = None,
) -> CommandResult:
    affected_list = affected if affected is not None else [to_command_target(t) for t in targets or []]
    scope = None
    if affected_list and action != "create":
        scope = "instance" if all(t.event_instance_id is not None for t in affected_list) else "series"
    return CommandResult(
        action=action,
        status=status,
        scope=scope,
        affected=affected_list,
        affected_count=len(affected_list),
        candidates=[to_command_target(t) for t in candidates or []],
        confirmation_token=pending.token if pending else None,
        expires_at=pending.expires_at if pending else None,
        action_id=action_id,
    )

