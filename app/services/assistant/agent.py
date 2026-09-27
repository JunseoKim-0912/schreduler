"""에이전트 루프와 세션·제안 관리 (docs/assistant_design.md §3, §5, §7).

한 턴: 사용자 말 저장 → LLM 호출 ↔ 도구 실행 반복(최대 6회, 턴 전체 20초) → 이번 턴의 성공한 초안을
PendingProposal 하나로 묶어 저장(30분 만료, 이전 pending은 superseded) → AssistantTurnLog 기록.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import local_now
from app.core.config import settings
from app.core.exceptions import ConflictError, ExpiredError, NotFoundError
from app.i18n import render_message
from app.models.assistant import AssistantMessage, AssistantSession, AssistantTurnLog, PendingProposal
from app.models.user import User
from app.services.assistant import prompt
from app.services.assistant.context import TurnContext
from app.services.assistant.execution import execute_proposal
from app.services.assistant.tools import TOOLS, run_tool
from app.services.llm_client import (
    LLMClientError,
    ResponsesCaller,
    ResponsesClient,
    ResponsesResult,
    resolve_reasoning_effort,
    with_tool_outputs,
)

logger = logging.getLogger(__name__)

MAX_LLM_CALLS = 6
TURN_TIME_LIMIT_SECONDS = 20.0
PROPOSAL_TTL = timedelta(minutes=30)


@dataclass
class ChatResult:
    session_id: int
    reply: str
    proposal: dict[str, Any] | None = None
    executed: list[dict[str, Any]] = field(default_factory=list)


# --- 세션 ------------------------------------------------------------------------------


def _wall(now: datetime | None) -> datetime:
    return (now or local_now()).replace(tzinfo=None)


def create_session(db: Session, user: User, now: datetime | None = None) -> AssistantSession:
    wall = _wall(now)
    session = AssistantSession(user_id=user.id, seen_ids={"events": [], "instances": []}, created_at=wall, updated_at=wall)
    db.add(session)
    db.flush()
    return session


def get_session(db: Session, user: User, session_id: int) -> AssistantSession:
    session = db.get(AssistantSession, session_id)
    if session is None or session.user_id != user.id:
        raise NotFoundError(f"assistant session {session_id} does not exist")
    return session


def current_session(db: Session, user: User, now: datetime | None = None) -> AssistantSession | None:
    """오늘(앱 시간대) 마지막으로 쓴 세션. 탭을 옮겨도 대화가 이어지게."""
    today_start = datetime.combine(_wall(now).date(), datetime.min.time())
    return db.execute(
        select(AssistantSession)
        .where(AssistantSession.user_id == user.id, AssistantSession.updated_at >= today_start)
        .order_by(AssistantSession.updated_at.desc(), AssistantSession.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def active_proposal(db: Session, session: AssistantSession, now: datetime | None = None) -> PendingProposal | None:
    """세션의 대기 중인 제안. 만료됐으면 expired로 바꾸고 없는 것으로 본다."""
    proposal = db.execute(
        select(PendingProposal)
        .where(PendingProposal.session_id == session.id, PendingProposal.status == "pending")
        .order_by(PendingProposal.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if proposal is not None and proposal.expires_at <= _wall(now):
        proposal.status = "expired"
        return None
    return proposal


def proposal_view(proposal: PendingProposal) -> dict[str, Any]:
    return {
        "token": proposal.token,
        "expires_at": proposal.expires_at,
        "items": prompt.card_items(proposal.proposals),
        "warnings": proposal.warnings,
    }


def display_messages(db: Session, session: AssistantSession) -> list[dict[str, Any]]:
    """화면에 다시 그릴 대화: 사용자 말과 어시스턴트의 최종 답만 (도구 호출·결과·추론은 뺀다)."""
    shown = []
    messages = db.execute(
        select(AssistantMessage).where(AssistantMessage.session_id == session.id).order_by(AssistantMessage.id)
    ).scalars()
    for message in messages:
        text = (message.content or {}).get("text")
        if message.role in ("user", "assistant") and text:
            shown.append({"role": message.role, "text": text, "created_at": message.created_at})
    return shown


def _add_message(db: Session, session: AssistantSession, role: str, content: dict[str, Any], wall: datetime) -> None:
    db.add(AssistantMessage(session_id=session.id, role=role, content=content, created_at=wall))


# --- 턴 -----------------------------------------------------------------------------------


@dataclass
class _Usage:
    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, result: ResponsesResult) -> None:
        self.calls += 1
        if result.usage is not None:
            self.input_tokens += result.usage.input_tokens
            self.cached_tokens += result.usage.cached_tokens
            self.output_tokens += result.usage.output_tokens
            self.reasoning_tokens += result.usage.reasoning_tokens


def _executed_text(ctx: TurnContext) -> str:
    return " ".join(render_message("command.executed", ctx.language, summary=item["summary"]) for item in ctx.executed)


def _fallback_reply(ctx: TurnContext, stop_reason: str | None) -> str:
    if stop_reason is not None:
        if ctx.drafts:
            return render_message("assistant.limit_with_drafts", ctx.language)
        if ctx.executed:
            return _executed_text(ctx)
        return render_message("assistant.limit_no_drafts", ctx.language)
    if ctx.drafts:
        return render_message("assistant.proposal_ready", ctx.language)
    if ctx.executed:
        return _executed_text(ctx)
    return render_message("assistant.empty_reply", ctx.language)


def _save_proposal(ctx: TurnContext) -> PendingProposal | None:
    if not ctx.drafts:
        return None
    for previous in ctx.db.execute(
        select(PendingProposal).where(PendingProposal.session_id == ctx.session.id, PendingProposal.status == "pending")
    ).scalars():
        previous.status = "superseded"
    items = [draft.to_json() for draft in ctx.drafts.values()]
    proposal = PendingProposal(
        session_id=ctx.session.id,
        token=secrets.token_urlsafe(16),
        proposals=items,
        warnings=[{"draft_id": item["draft_id"], **warning} for item in items for warning in item["card"]["warnings"]],
        shown_at=ctx.wall_now,
        expires_at=ctx.wall_now + PROPOSAL_TTL,
        status="pending",
    )
    ctx.db.add(proposal)
    return proposal


def chat(
    db: Session,
    user: User,
    message: str,
    session_id: int | None = None,
    *,
    client: ResponsesCaller | None = None,
    now: datetime | None = None,
    timer: Callable[[], float] = time.monotonic,
) -> ChatResult:
    now = now or local_now()
    session = get_session(db, user, session_id) if session_id is not None else create_session(db, user, now)
    ctx = TurnContext(db=db, user=user, session=session, now=now)
    client = client or ResponsesClient()

    history = prompt.history_items(
        list(db.execute(select(AssistantMessage).where(AssistantMessage.session_id == session.id).order_by(AssistantMessage.id)).scalars())
    )
    pending = active_proposal(db, session, now)
    session.updated_at = ctx.wall_now
    _add_message(db, session, "user", {"text": message}, ctx.wall_now)
    db.commit()

    instructions = prompt.instruction_blocks()
    items = prompt.turn_input(
        context_block=prompt.ranges_and_locations_block(db, user.id, user.preferred_language),
        time_block=prompt.time_block(now),
        history=history,
        pending=prompt.pending_block(pending),
        message=message,
    )

    usage = _Usage()
    started = timer()
    stop_reason: str | None = None
    reply = ""
    last: ResponsesResult | None = None
    while True:
        # 호출 한 번의 타임아웃(ResponsesClient)과 별개로 턴 전체 시간을 잰다.
        if usage.calls >= MAX_LLM_CALLS:
            stop_reason = "llm_call_limit"
            break
        if timer() - started >= TURN_TIME_LIMIT_SECONDS:
            stop_reason = "time_limit"
            break
        try:
            result = client.create(task="assistant", instruction_blocks=instructions, tools=TOOLS, input_items=items)
        except LLMClientError:
            if usage.calls == 0:
                _log_turn(ctx, usage, last, timer() - started, "llm_error")
                db.commit()
                raise
            logger.warning("[assistant] LLM call failed mid-turn; answering with what we have", exc_info=True)
            stop_reason = "llm_error"
            break
        usage.add(result)
        last = result
        _add_message(
            db, session, "assistant", {"output": result.output_items, "text": result.text, "response_id": result.response_id}, ctx.wall_now
        )
        if not result.tool_calls:
            reply = result.text.strip()
            break
        outputs: dict[str, object] = {}
        for tool_call in result.tool_calls:
            output = run_tool(ctx, tool_call)
            outputs[tool_call.call_id] = output
            _add_message(
                db,
                session,
                "tool",
                {"call_id": tool_call.call_id, "name": tool_call.name, "output": json.dumps(output, ensure_ascii=False, default=str)},
                ctx.wall_now,
            )
        # 턴 안에서는 직전 응답의 output 전체(암호화된 reasoning 포함)를 그대로 다시 넣는다.
        items = with_tool_outputs(items, result, outputs)

    if stop_reason is not None or not reply:
        reply = _fallback_reply(ctx, stop_reason)
        _add_message(db, session, "assistant", {"output": [], "text": reply, "fallback": stop_reason or "empty"}, ctx.wall_now)

    proposal = _save_proposal(ctx)
    _log_turn(ctx, usage, last, timer() - started, stop_reason)
    db.commit()
    return ChatResult(
        session_id=session.id,
        reply=reply,
        proposal=proposal_view(proposal) if proposal is not None else None,
        executed=ctx.executed,
    )


def _log_turn(ctx: TurnContext, usage: _Usage, last: ResponsesResult | None, elapsed: float, stop_reason: str | None) -> None:
    model = (last.model if last and last.model else None) or settings.assistant_model
    effort = (last.reasoning_effort if last and last.reasoning_effort else None) or resolve_reasoning_effort(
        settings.assistant_model, settings.assistant_reasoning_effort
    )
    log = AssistantTurnLog(
        session_id=ctx.session.id,
        llm_calls=usage.calls,
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        latency_ms=round(elapsed * 1000),
        model=model,
        reasoning_effort=effort,
        stop_reason=stop_reason,
        created_at=ctx.wall_now,
    )
    ctx.db.add(log)
    logger.info(
        "[assistant turn] session=%s calls=%d input=%d cached=%d output=%d reasoning=%d latency_ms=%d stop=%s",
        ctx.session.id,
        usage.calls,
        usage.input_tokens,
        usage.cached_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        log.latency_ms,
        stop_reason,
    )


# --- 버튼 확인·취소 ----------------------------------------------------------------------


def _proposal_for_button(db: Session, user: User, session_id: int, token: str, now: datetime | None) -> tuple[AssistantSession, PendingProposal]:
    session = get_session(db, user, session_id)
    proposal = db.execute(
        select(PendingProposal).where(PendingProposal.session_id == session.id, PendingProposal.token == token)
    ).scalar_one_or_none()
    if proposal is None:
        raise NotFoundError("proposal not found in this session")
    if proposal.status == "pending" and proposal.expires_at <= _wall(now):
        proposal.status = "expired"
        db.commit()
    if proposal.status == "expired":
        raise ExpiredError("the proposal expired (30 minutes) — please ask again")
    if proposal.status != "pending":
        raise ConflictError(f"the proposal is already {proposal.status}")
    return session, proposal


def confirm(db: Session, user: User, session_id: int, token: str, now: datetime | None = None) -> ChatResult:
    session, proposal = _proposal_for_button(db, user, session_id, token, now)
    executed = execute_proposal(db, user, proposal.proposals)
    wall = _wall(now)
    proposal.status = "confirmed"
    reply = " ".join(render_message("command.executed", user.preferred_language, summary=item["summary"]) for item in executed)
    _add_message(db, session, "assistant", {"output": [], "text": reply, "source": "confirm_button"}, wall)
    session.updated_at = wall
    db.commit()
    return ChatResult(session_id=session.id, reply=reply, executed=executed)


def cancel(db: Session, user: User, session_id: int, token: str, now: datetime | None = None) -> ChatResult:
    session, proposal = _proposal_for_button(db, user, session_id, token, now)
    wall = _wall(now)
    proposal.status = "cancelled"
    reply = render_message("assistant.cancelled", user.preferred_language)
    _add_message(db, session, "assistant", {"output": [], "text": reply, "source": "cancel_button"}, wall)
    session.updated_at = wall
    db.commit()
    return ChatResult(session_id=session.id, reply=reply)
