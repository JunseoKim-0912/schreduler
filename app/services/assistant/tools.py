"""LLM에게 주는 도구 정의와 실행. 도구는 읽기·제안만 한다. confirm_pending만 예외이며 §5 조건을 통과해야 실행된다."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from app.core.exceptions import AppError
from app.models.assistant import PendingProposal
from app.services.assistant import drafts
from app.services.assistant.context import TurnContext
from app.services.assistant.execution import execute_proposal
from app.services.assistant.search import search_events
from app.services.llm_client import FunctionTool, ToolCall

logger = logging.getLogger(__name__)

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    """strict 모드: 모든 속성을 required로, 선택 값은 null을 허용해서 표현한다."""
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        return {"anyOf": [schema, {"type": "null"}]}
    nullable = {**schema, "type": [schema["type"], "null"]}
    if "enum" in schema:
        nullable["enum"] = [*schema["enum"], None]
    return nullable


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


_DATE = "YYYY-MM-DD"
_HHMM = "HH:MM, 24-hour"
_INFERRED = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Names of fields you filled by inference rather than from the user's words (e.g. end_time, importance, date_range).",
}
_DRAFT_ID = _nullable(_str("To revise a draft you already proposed in THIS turn, its draft_id; otherwise null."))
_TARGETS = {
    "type": "array",
    "items": _obj({"event_id": {"type": "integer"}, "instance_id": _nullable({"type": "integer"})}),
    "description": "Targets exactly as returned by search_events in this conversation. Never invent ids.",
}
_LOCATION = _obj(
    {
        "name": _str("Location name. An existing name reuses it; a new name registers a new location."),
        "travel_minutes": _nullable({"type": "integer", "description": "Travel time in minutes for a new location; estimate if unknown and list it in inferred_fields."}),
    }
)

TOOLS: list[FunctionTool] = [
    FunctionTool(
        name="search_events",
        description=(
            "Find the user's existing events. query matches titles loosely (case, spaces, typos). With date_from/date_to "
            "(max 62 days) it returns occurrences (instance_id set) on those dates; without dates it returns whole events "
            "(instance_id null). weekday filters by day. Only ids returned here may be used as targets."
        ),
        parameters=_obj(
            {
                "query": _nullable(_str("Title words, e.g. 'ECE360 Lecture'")),
                "date_from": _nullable(_str(_DATE)),
                "date_to": _nullable(_str(_DATE)),
                "weekday": _nullable({"type": "string", "enum": WEEKDAYS}),
            }
        ),
    ),
    FunctionTool(
        name="propose_create_event",
        description=(
            "Draft a new event (not saved). One-off: pass date. Repeating: pass recurrence and date_range. A deadline has "
            "no start_time; end_time is the due time. Returns the normalized draft, errors (fix and call again), "
            "warnings, preview_dates and time_display."
        ),
        parameters=_obj(
            {
                "event_type": {"type": "string", "enum": ["scheduled", "deadline"]},
                "title": _str("Event title in the user's words"),
                "date": _nullable(_str(f"{_DATE}. Required for one-off events.")),
                "start_time": _nullable(_str(f"{_HHMM}. null for deadlines.")),
                "end_time": _nullable(_str(f"{_HHMM}. Due time for deadlines. null → 1 hour after start (deadline: 23:59).")),
                "importance": _nullable({"type": "integer", "enum": [1, 2, 3, 4, 5, 6]}),
                "recurrence": _nullable(
                    _obj(
                        {
                            "frequency": {"type": "string", "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"]},
                            "interval": {"type": "integer", "description": "1 = every week, 2 = every other week"},
                            "by_day": {"type": "array", "items": {"type": "string", "enum": WEEKDAYS}},
                            "start_date": _nullable(_str(f"{_DATE}. First occurrence if the user said one; else null.")),
                        }
                    )
                ),
                "date_range": _nullable(
                    _obj(
                        {
                            "name": _str("Existing period name, or the name of a new period"),
                            "start_date": _nullable(_str(f"{_DATE}, only for a new period")),
                            "end_date": _nullable(_str(f"{_DATE}, only for a new period")),
                        }
                    )
                ),
                "location": _nullable(_LOCATION),
                "inferred_fields": _INFERRED,
                "draft_id": _DRAFT_ID,
            }
        ),
    ),
    FunctionTool(
        name="propose_update_event",
        description=(
            "Draft changes to existing events found with search_events (not saved). scope=instance changes only the given "
            "occurrences; series changes the whole event. Location is always series-wide. Leave unchanged fields null."
        ),
        parameters=_obj(
            {
                "target_ids": _TARGETS,
                "scope": {"type": "string", "enum": ["instance", "series"]},
                "changes": _obj(
                    {
                        "title": _nullable(_str("New title")),
                        "date": _nullable(_str(f"{_DATE}. Moves a one-off event to this date.")),
                        "start_time": _nullable(_str(f"{_HHMM}. Changing only the start keeps the duration.")),
                        "end_time": _nullable(_str(f"{_HHMM}. For deadlines: the new due time.")),
                        "importance": _nullable({"type": "integer", "enum": [1, 2, 3, 4, 5, 6]}),
                        "location": _nullable(
                            _obj(
                                {
                                    "action": {"type": "string", "enum": ["set", "remove"]},
                                    "name": _nullable(_str("Location name for set")),
                                    "travel_minutes": _nullable({"type": "integer"}),
                                }
                            )
                        ),
                    }
                ),
                "inferred_fields": _INFERRED,
                "draft_id": _DRAFT_ID,
            }
        ),
    ),
    FunctionTool(
        name="propose_delete_event",
        description="Draft deleting events found with search_events (not saved). scope=instance deletes only those occurrences.",
        parameters=_obj(
            {
                "target_ids": _TARGETS,
                "scope": {"type": "string", "enum": ["instance", "series"]},
                "inferred_fields": _INFERRED,
                "draft_id": _DRAFT_ID,
            }
        ),
    ),
    FunctionTool(
        name="propose_date_range",
        description=(
            "Draft creating, updating or deleting a repeat period (e.g. 'Lecture Period'). update/delete use an existing "
            "name. Deleting a period in use: mode range_only keeps events (they stop at the last occurrence), with_events "
            "deletes them too."
        ),
        parameters=_obj(
            {
                "action": {"type": "string", "enum": ["create", "update", "delete"]},
                "name": _str("Period name (existing one for update/delete)"),
                "new_name": _nullable(_str("New name, update only")),
                "start_date": _nullable(_str(_DATE)),
                "end_date": _nullable(_str(_DATE)),
                "mode": _nullable({"type": "string", "enum": ["range_only", "with_events"]}),
                "inferred_fields": _INFERRED,
                "draft_id": _DRAFT_ID,
            }
        ),
    ),
    FunctionTool(
        name="confirm_pending",
        description=(
            "Execute the pending proposal shown in an EARLIER turn, only when the user's latest message approves it "
            "(e.g. '좋아', '만들어줘', 'yes'). Never call it for drafts made in this turn."
        ),
        parameters=_obj({"token": _str("The token of the pending proposal")}),
    ),
]

TOOL_NAMES = {tool.name for tool in TOOLS}


def confirm_pending(ctx: TurnContext, args: dict[str, Any]) -> dict[str, Any]:
    """§5: 이전 턴에 사용자에게 보여졌고, pending이고, 만료되지 않은 제안만 실행한다."""
    if ctx.drafts:
        return {"errors": [{"code": "self_approval", "message": "Drafts made in this turn must be shown to the user and approved in a later turn."}]}
    proposal = ctx.db.execute(
        select(PendingProposal).where(PendingProposal.token == str(args.get("token")), PendingProposal.session_id == ctx.session.id)
    ).scalar_one_or_none()
    if proposal is None:
        return {"errors": [{"code": "unknown_token", "message": "No pending proposal with this token in this conversation."}]}
    if proposal.shown_at > ctx.turn_started_at:
        return {"errors": [{"code": "self_approval", "message": "This proposal was not shown to the user before this turn."}]}
    if proposal.status == "pending" and proposal.expires_at <= ctx.wall_now:
        proposal.status = "expired"
    if proposal.status != "pending":
        return {"errors": [{"code": f"proposal_{proposal.status}", "message": f"The proposal is {proposal.status}; propose again if needed."}]}
    ctx.db.commit()  # 실행이 실패해 롤백돼도 이번 턴의 대화 기록은 남도록
    try:
        executed = execute_proposal(ctx.db, ctx.user, proposal.proposals)
    except AppError as exc:
        return {"errors": [{"code": "execution_failed", "message": str(exc)}]}
    proposal.status = "confirmed"
    ctx.executed.extend(executed)
    return {"executed": executed, "note": "Saved. Tell the user briefly what was done."}


_HANDLERS: dict[str, Callable[[TurnContext, dict[str, Any]], dict[str, Any]]] = {
    "search_events": search_events,
    "propose_create_event": drafts.propose_create_event,
    "propose_update_event": drafts.propose_update_event,
    "propose_delete_event": drafts.propose_delete_event,
    "propose_date_range": drafts.propose_date_range,
    "confirm_pending": confirm_pending,
}


def run_tool(ctx: TurnContext, tool_call: ToolCall) -> dict[str, Any]:
    """도구 하나 실행. 규칙 위반·잘못된 값(ValueError 계열, AppError)은 500으로 새지 않고 errors로 LLM에게 돌아간다."""
    handler = _HANDLERS.get(tool_call.name)
    if handler is None:
        return {"errors": [{"code": "unknown_tool", "message": f"no tool named {tool_call.name}"}]}
    try:
        args = json.loads(tool_call.arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as exc:
        return {"errors": [{"code": "invalid_arguments", "message": str(exc)}]}
    try:
        return handler(ctx, args)
    except (ValueError, TypeError, KeyError, AppError) as exc:
        logger.info("[assistant] tool %s rejected: %s: %s", tool_call.name, type(exc).__name__, exc)
        return {"errors": [{"code": "invalid_value", "message": f"{type(exc).__name__}: {exc}"}]}
