"""확정된 제안 실행. 기록이 남는 서비스 함수(create_event_from_nl, execute, date_range_command_service)만 쓰고,
제안 하나에 든 여러 초안은 한 트랜잭션으로 커밋한다 — 중간에 하나라도 실패하면 아무것도 남지 않는다."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError
from app.models.action_history import ActionHistory
from app.models.enums import ActionSource, EventInstanceStatus
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.event import EventCreate
from app.schemas.event_command import NewDateRangeDraft, NewLocationDraft
from app.services import date_range_command_service as ranges
from app.services.action_history_service import AfterCommit
from app.services.event_command_service import CommandDescription, Target, create_event_from_nl, execute

# 새 기간을 먼저 만들어야 같은 제안의 이벤트가 그 기간을 이름으로 찾아 쓴다.
_ORDER = {"create_range": 0, "update_range": 1, "create_event": 2, "update_event": 3, "delete_event": 4, "delete_range": 5}


def _changed() -> ConflictError:
    return ConflictError("the targets changed after the proposal was made — please ask again")


def _range(db: Session, user: User, range_id: int) -> ImportantDateRange:
    date_range = db.get(ImportantDateRange, range_id)
    if date_range is None or date_range.user_id != user.id:
        raise _changed()
    return date_range


def _targets(db: Session, user: User, raw: list[list[int | None]]) -> list[Target]:
    targets = []
    for event_id, instance_id in raw:
        event = db.get(Event, event_id)
        instance = db.get(EventInstance, instance_id) if instance_id is not None else None
        if event is None or event.user_id != user.id:
            raise _changed()
        if instance_id is not None and (instance is None or instance.status == EventInstanceStatus.CANCELLED):
            raise _changed()
        targets.append(Target(event, instance))
    return targets


def _run(db: Session, user: User, kind: str, payload: dict[str, Any], defer: list[AfterCommit]) -> ActionHistory:
    if kind == "create_event":
        new_range = payload.get("new_date_range")
        new_location = payload.get("new_location")
        return create_event_from_nl(
            db,
            user,
            EventCreate(**payload["event"]),
            NewDateRangeDraft(**new_range) if new_range else None,
            NewLocationDraft(**new_location) if new_location else None,
            defer=defer,
        ).action
    if kind in ("update_event", "delete_event"):
        desc = CommandDescription.from_dict(payload["description"])
        return execute(db, user, desc, _targets(db, user, payload["targets"]), ActionSource.NL, defer=defer).action
    if kind == "create_range":
        start, end = date.fromisoformat(payload["start_date"]), date.fromisoformat(payload["end_date"])
        return ranges.create_range(db, user, payload["name"], start, end, ActionSource.NL, defer=defer)[1]
    if kind == "update_range":
        return ranges.update_range(
            db,
            user,
            _range(db, user, payload["range_id"]),
            name=payload.get("new_name"),
            start=date.fromisoformat(payload["start_date"]),
            end=date.fromisoformat(payload["end_date"]),
            source=ActionSource.NL,
            defer=defer,
        ).action
    if kind == "delete_range":
        return ranges.delete_range(db, user, _range(db, user, payload["range_id"]), payload.get("mode"), ActionSource.NL, defer=defer)
    raise ValueError(f"unknown draft kind: {kind}")


def execute_proposal(db: Session, user: User, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{action_id, summary}]. 되돌리기는 action_id별로 POST /actions/{id}/undo."""
    defer: list[AfterCommit] = []
    actions: list[ActionHistory] = []
    try:
        for item in sorted(items, key=lambda it: _ORDER.get(it["kind"], 9)):
            actions.append(_run(db, user, item["kind"], item["payload"], defer))
        executed = [{"action_id": action.id, "summary": action.summary_text} for action in actions]
        db.commit()
    except Exception:
        db.rollback()
        raise
    for after in defer:
        after.run(db)
    return executed
