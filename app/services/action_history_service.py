"""FR-2 v3.6 변경 기록과 되돌리기.

변경을 실행하는 쪽(event_command_service, DELETE /events/{id})은 같은 트랜잭션 안에서
1) snapshot_*()로 변경 전 행을 뜨고 2) 변경을 적용한 뒤 3) record_action()으로 기록하고 한 번에 커밋한다.

스냅샷은 관련 행의 컬럼 값을 그대로 JSON으로 담는다: {"events": [...], "event_instances": [...],
"compliance_reports": [...]}. 되돌리기는 이 값으로 행을 덮어쓰거나(수정·회차 취소), 원래 ID 그대로 다시
넣는다(삭제). 생성은 스냅샷 대신 affected_ids["created_events"]를 지운다.
"""

from __future__ import annotations

import enum
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import DateTime, Enum, inspect, select
from sqlalchemy import Date as SADate
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.i18n import render_message
from app.models.action_history import ActionHistory
from app.models.compliance_report import ComplianceReport
from app.models.enums import ActionSource, ActionType
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.user import User
from app.services.event_service import remove_event
from app.services.points import recalculate_points_since

logger = logging.getLogger(__name__)

# 복원할 때 이 순서로 넣어야 FK(events ← event_instances ← compliance_reports)가 맞는다.
SNAPSHOT_TABLES: dict[str, type[Any]] = {
    "events": Event,
    "event_instances": EventInstance,
    "compliance_reports": ComplianceReport,
}


# --- 행 ↔ JSON ------------------------------------------------------------------


def _encode(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.name  # DB에 저장되는 값과 같은 형태(멤버 이름)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def row_to_dict(obj: Any) -> dict[str, Any]:
    return {attr.key: _encode(getattr(obj, attr.key)) for attr in inspect(obj).mapper.column_attrs}


def _decode_row(model: type[Any], data: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for attr in inspect(model).column_attrs:
        if attr.key not in data:
            continue
        raw = data[attr.key]
        column_type = attr.columns[0].type
        if raw is None:
            values[attr.key] = None
        elif isinstance(column_type, Enum) and column_type.enum_class is not None:
            values[attr.key] = column_type.enum_class[raw]
        elif isinstance(column_type, DateTime):
            values[attr.key] = datetime.fromisoformat(raw)
        elif isinstance(column_type, SADate):
            values[attr.key] = date.fromisoformat(raw)
        else:
            values[attr.key] = raw
    return values


# --- 스냅샷 -----------------------------------------------------------------------


class Snapshot:
    """변경 전 행 모음. 같은 행을 여러 번 넣어도 한 번만 담긴다."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[int, dict[str, Any]]] = {table: {} for table in SNAPSHOT_TABLES}

    def add(self, table: str, obj: Any) -> None:
        self._rows[table].setdefault(obj.id, row_to_dict(obj))

    def add_event_tree(self, event: Event) -> None:
        """이벤트를 지울 때 함께 사라지는 것 전부: 이벤트, 하위 일정, 그 회차들, 회차의 미준수 사유."""
        for item in [event, *event.child_events]:
            self.add("events", item)
            for instance in item.instances:
                self.add_instance(instance)

    def add_instance(self, instance: EventInstance) -> None:
        self.add("event_instances", instance)
        for report in instance.compliance_reports:
            self.add("compliance_reports", report)

    def add_event_with_children(self, event: Event) -> None:
        """시리즈 수정 때 바뀌는 행: 이벤트와 (시각이 함께 밀리는) 하위 일정."""
        for item in [event, *event.child_events]:
            self.add("events", item)

    def to_json(self) -> dict[str, list[dict[str, Any]]]:
        return {table: list(rows.values()) for table, rows in self._rows.items()}


def record_action(
    db: Session,
    *,
    user_id: int,
    action_type: ActionType,
    source: ActionSource,
    summary_text: str,
    snapshot: Snapshot | None,
    affected_ids: dict[str, list[int]],
) -> ActionHistory:
    """변경 기록을 세션에 추가한다 (커밋하지 않음 — 변경과 같은 트랜잭션에서 커밋되어야 한다)."""
    action = ActionHistory(
        user_id=user_id,
        action_type=action_type,
        source=source,
        summary_text=summary_text[:500],
        snapshot_before=snapshot.to_json() if snapshot else {table: [] for table in SNAPSHOT_TABLES},
        affected_ids=affected_ids,
    )
    db.add(action)
    return action


def recalculate_points_for_dates(db: Session, user_id: int, dates: list[date]) -> None:
    """지난 날짜의 회차가 바뀌었으면 그날부터 어제까지 포인트 원장을 다시 계산한다 (늦은 완료와 같은 로직)."""
    past = [day for day in dates if day < date.today()]
    if past:
        recalculate_points_since(db, user_id, min(past))


# --- 되돌리기 ------------------------------------------------------------------------


def _event_ids(action: ActionHistory) -> set[int]:
    ids = action.affected_ids or {}
    return set(ids.get("events", [])) | set(ids.get("created_events", []))


def _restore(db: Session, snapshot: dict[str, list[dict[str, Any]]]) -> list[date]:
    """스냅샷 값으로 행을 덮어쓰거나, 지워졌으면 원래 ID 그대로 다시 넣는다. 영향받은 회차 날짜를 돌려준다."""
    # 부모 이벤트가 먼저 들어가야 하위 일정의 parent_event_id가 맞는다.
    events = sorted(snapshot.get("events", []), key=lambda row: row.get("parent_event_id") is not None)
    ordered = {"events": events, **{t: snapshot.get(t, []) for t in SNAPSHOT_TABLES if t != "events"}}

    touched_dates: list[date] = []
    for table, model in SNAPSHOT_TABLES.items():
        for row in ordered[table]:
            values = _decode_row(model, row)
            existing = db.get(model, values["id"])
            if existing is None:
                db.add(model(**values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            if table == "event_instances":
                touched_dates.append(values["date"])
        db.flush()
    return touched_dates


def _undo_create(db: Session, action: ActionHistory) -> list[date]:
    touched_dates: list[date] = []
    for event_id in action.affected_ids.get("created_events", []):
        event = db.get(Event, event_id)
        if event is None:
            continue
        for item in [event, *event.child_events]:
            touched_dates.extend(instance.date for instance in item.instances)
        remove_event(db, event)
    return touched_dates


def undo_action(db: Session, user: User, action_id: int) -> ActionHistory:
    action = db.get(ActionHistory, action_id)
    if action is None or action.user_id != user.id:
        raise NotFoundError(f"action {action_id} does not exist")
    if action.undone_at is not None:
        raise ConflictError(render_message("undo.already_undone", user.preferred_language))

    # 이 기록 뒤에 같은 이벤트를 건드린, 아직 되돌리지 않은 기록이 있으면 순서가 꼬이므로 거절한다.
    newer = db.execute(
        select(ActionHistory).where(
            ActionHistory.user_id == user.id,
            ActionHistory.id > action.id,
            ActionHistory.undone_at.is_(None),
        )
    ).scalars()
    if any(_event_ids(later) & _event_ids(action) for later in newer):
        raise ConflictError(render_message("undo.newer_change_exists", user.preferred_language))

    if action.action_type == ActionType.CREATE:
        touched_dates = _undo_create(db, action)
    else:
        touched_dates = _restore(db, action.snapshot_before)

    action.undone_at = datetime.now()
    db.commit()
    db.refresh(action)
    logger.info("[되돌리기] user_id=%s action_id=%s (%s)", user.id, action.id, action.summary_text)

    recalculate_points_for_dates(db, user.id, touched_dates)
    return action


def list_actions(db: Session, user_id: int, limit: int) -> list[ActionHistory]:
    return list(
        db.execute(
            select(ActionHistory)
            .where(ActionHistory.user_id == user_id)
            .order_by(ActionHistory.id.desc())
            .limit(limit)
        ).scalars()
    )
