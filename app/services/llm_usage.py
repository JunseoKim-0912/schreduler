"""LLM usage logging and the daily spend cap.

Every request that reaches the LLM API runs inside usage_scope(): post_chat_completion and post_responses call
ensure_budget() right before the HTTP request and record_llm_call() after a successful one. A call outside a
scope is refused, so a new call site can't silently skip logging or the cap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal

from fastapi import status
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.core.clock import app_timezone, local_now, utc_now_naive
from app.core.config import settings
from app.core.exceptions import AppError
from app.i18n import MessageKey, render_message, timezone_city
from app.models.llm_usage_log import LlmUsageLog
from app.models.user import User
from app.services.llm_pricing import call_cost

logger = logging.getLogger(__name__)

LlmFeature = Literal["assistant", "checkin", "compliance", "script"]
BudgetReason = Literal["user_limit", "total_limit"]


class MissingUsageScopeError(AppError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    log_level = logging.ERROR


class LlmBudgetExceeded(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, message: str, *, reason: BudgetReason, spent_usd: float, limit_usd: float, resets_at: datetime) -> None:
        super().__init__(message)
        self.reason = reason
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.resets_at = resets_at

    def response_fields(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "spent_usd": round(self.spent_usd, 6),
            "limit_usd": self.limit_usd,
            "resets_at": self.resets_at.isoformat(),
        }


@dataclass
class _UsageScope:
    db: Session
    user_id: int | None
    feature: LlmFeature
    rows: list[LlmUsageLog] = field(default_factory=list)


_scope: ContextVar[_UsageScope | None] = ContextVar("llm_usage_scope", default=None)


@contextmanager
def usage_scope(db: Session, user_id: int | None, feature: LlmFeature) -> Iterator[None]:
    scope = _UsageScope(db, user_id, feature)
    token = _scope.set(scope)
    try:
        yield
        if scope.rows:
            # Not every caller commits (a check-in without a persona saves nothing else).
            db.commit()
    except BaseException:
        # The caller's session is about to be rolled back with the failed request, but the API calls already
        # made were paid for — keep their rows.
        if scope.rows:
            db.rollback()
            lost = [row for row in scope.rows if inspect(row).transient]
            if lost:
                db.add_all(lost)
                db.commit()
        raise
    finally:
        _scope.reset(token)


def _current_scope() -> _UsageScope:
    scope = _scope.get()
    if scope is None:
        raise MissingUsageScopeError("LLM call outside usage_scope() — wrap the caller so the call is logged and capped")
    return scope


@dataclass(frozen=True)
class DayWindow:
    start_utc: datetime
    end_utc: datetime
    resets_at: datetime  # next APP_TIMEZONE midnight, timezone-aware


def day_window(now: datetime | None = None) -> DayWindow:
    zone = app_timezone()
    current = (now or local_now()).astimezone(zone)
    start = datetime.combine(current.date(), time.min, tzinfo=zone)
    end = datetime.combine(current.date() + timedelta(days=1), time.min, tzinfo=zone)
    return DayWindow(
        start_utc=start.astimezone(UTC).replace(tzinfo=None),
        end_utc=end.astimezone(UTC).replace(tzinfo=None),
        resets_at=end,
    )


def spent_usd(db: Session, window: DayWindow, user_id: int | None = None) -> float:
    query = select(func.coalesce(func.sum(LlmUsageLog.cost_usd), 0.0)).where(
        LlmUsageLog.created_at >= window.start_utc, LlmUsageLog.created_at < window.end_utc
    )
    if user_id is not None:
        query = query.where(LlmUsageLog.user_id == user_id)
    return float(db.scalar(query) or 0.0)


def user_limit_usd(user: User | None) -> float:
    if user is not None and user.is_admin and settings.llm_daily_budget_admin_usd is not None:
        return settings.llm_daily_budget_admin_usd
    return settings.llm_daily_budget_per_user_usd


@dataclass(frozen=True)
class BudgetStatus:
    spent_usd: float
    limit_usd: float
    total_spent_usd: float
    total_limit_usd: float
    resets_at: datetime

    @property
    def user_blocked(self) -> bool:
        return self.spent_usd >= self.limit_usd

    @property
    def total_blocked(self) -> bool:
        return self.total_spent_usd >= self.total_limit_usd


def budget_status(db: Session, user: User | None, now: datetime | None = None) -> BudgetStatus:
    window = day_window(now)
    return BudgetStatus(
        spent_usd=spent_usd(db, window, user.id) if user is not None else 0.0,
        limit_usd=user_limit_usd(user),
        total_spent_usd=spent_usd(db, window),
        total_limit_usd=settings.llm_daily_budget_total_usd,
        resets_at=window.resets_at,
    )


def check_budget(db: Session, user: User | None) -> None:
    """Raise LlmBudgetExceeded if this user's or everyone's spend today has reached its cap."""
    current = budget_status(db, user)
    if user is not None and current.user_blocked:
        reason: BudgetReason = "user_limit"
        spent, limit = current.spent_usd, current.limit_usd
    elif current.total_blocked:
        reason, spent, limit = "total_limit", current.total_spent_usd, current.total_limit_usd
    else:
        return
    language = user.preferred_language if user is not None else None
    key: MessageKey = "llm_budget.user_limit" if reason == "user_limit" else "llm_budget.total_limit"
    logger.info("[LLM budget] blocked user_id=%s reason=%s spent=%.4f limit=%.2f", user.id if user else None, reason, spent, limit)
    raise LlmBudgetExceeded(
        render_message(key, language, city=timezone_city(settings.app_timezone, language)),
        reason=reason,
        spent_usd=spent,
        limit_usd=limit,
        resets_at=current.resets_at,
    )


def ensure_budget() -> None:
    scope = _current_scope()
    user = scope.db.get(User, scope.user_id) if scope.user_id is not None else None
    check_budget(scope.db, user)


def record_llm_call(model: str, input_tokens: int, cached_tokens: int, output_tokens: int, reasoning_tokens: int) -> LlmUsageLog:
    scope = _current_scope()
    row = LlmUsageLog(
        user_id=scope.user_id,
        feature=scope.feature,
        model=model,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=call_cost(model, input_tokens, cached_tokens, output_tokens),
        created_at=utc_now_naive(),
    )
    scope.db.add(row)
    scope.db.flush()
    scope.rows.append(row)
    return row
