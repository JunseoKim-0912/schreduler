"""One-click demo accounts: created by POST /auth/demo, deleted with everything they own once they expire.

A demo account has no email or password, so the only way into it is the session cookie handed out when it is made.
The hourly cleanup deletes only rows owned by expired demo users; tests/test_demo.py checks that nobody else's rows
are touched.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

from fastapi import status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.clock import local_today, utc_now_naive
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.exceptions import AppError
from app.core.scheduler import scheduler
from app.i18n import Language, render_message
from app.models.action_history import ActionHistory
from app.models.assistant import AssistantMessage, AssistantSession, AssistantTurnLog, PendingProposal
from app.models.auth import UserSession
from app.models.compliance_report import ComplianceReport
from app.models.daily_actual_log import DailyActualLog
from app.models.engagement_state import EngagementState
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.models.important_date_range import ImportantDateRange
from app.models.llm_usage_log import LlmUsageLog
from app.models.location import Location
from app.models.persona_conversation import PersonaConversation
from app.models.points_ledger import PointsLedger
from app.models.sleep_log import SleepLog
from app.models.user import User
from app.services import auth_service
from app.services.demo_seed import seed_demo_week

logger = logging.getLogger(__name__)

DEMO_TTL = timedelta(hours=24)
CREATION_WINDOW = timedelta(hours=1)
DEMO_NAME = "Demo student"
CLEANUP_JOB_ID = "demo_cleanup"


class TooManyDemoAccounts(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

    def response_fields(self) -> dict[str, Any]:
        return {"retry_after_seconds": self.retry_after_seconds}


def _check_creation_rate(db: Session, now: datetime, language: Language) -> None:
    """At most DEMO_MAX_CREATIONS_PER_HOUR demo accounts in any hour, across everyone. Not per IP: behind the proxy
    the address can't be trusted, and a global cap is what bounds the cost."""
    recent = list(
        db.execute(
            select(User.created_at).where(User.is_demo.is_(True), User.created_at > now - CREATION_WINDOW).order_by(User.created_at)
        ).scalars()
    )
    limit = settings.demo_max_creations_per_hour
    if len(recent) >= limit:
        reopens = recent[len(recent) - limit] + CREATION_WINDOW if limit > 0 else now + CREATION_WINDOW
        seconds = max(int((reopens - now).total_seconds()), 1)
        logger.info("[demo] creation refused: %d accounts in the last hour", len(recent))
        raise TooManyDemoAccounts(render_message("demo.rate_limited", language, minutes=math.ceil(seconds / 60)), seconds)


def create_demo_user(db: Session, language: Language, now: datetime | None = None) -> tuple[User, str]:
    """A new demo account with a week of sample data, signed in. Returns the user and the session cookie token."""
    now = now or utc_now_naive()
    _check_creation_rate(db, now, language)
    user = User(
        name=DEMO_NAME,
        email=None,
        password_hash=None,
        preferred_language=language,
        is_demo=True,
        demo_expires_at=now + DEMO_TTL,
        created_at=now,
        last_login_at=now,
    )
    db.add(user)
    db.flush()
    seed_demo_week(db, user, local_today())
    token = auth_service.start_session(db, user, now)
    db.commit()
    logger.info("[demo] created user_id=%s expires_at=%s", user.id, user.demo_expires_at)
    return user, token


def purge_demo_users(db: Session, user_ids: list[int]) -> None:
    """Deletes these demo users and every row they own. Usage rows are kept but detached (user_id NULL, is_demo stays
    true): deleting them would lower today's demo total mid-day and drop real spend from the cost reports."""
    if not user_ids:
        return
    # Guard against a caller passing a real account: only ids that really are demo users go any further.
    user_ids = list(db.execute(select(User.id).where(User.id.in_(user_ids), User.is_demo.is_(True))).scalars())
    if not user_ids:
        return
    owned_events = select(Event.id).where(Event.user_id.in_(user_ids))
    owned_instances = select(EventInstance.id).where(EventInstance.event_id.in_(owned_events))
    owned_sessions = select(AssistantSession.id).where(AssistantSession.user_id.in_(user_ids))

    statements = [
        delete(ComplianceReport).where(ComplianceReport.event_instance_id.in_(owned_instances)),
        delete(EngagementState).where(EngagementState.user_id.in_(user_ids)),
        delete(EventInstance).where(EventInstance.event_id.in_(owned_events)),
        # Children point at their parent; unhook them so the delete doesn't depend on row order.
        update(Event).where(Event.user_id.in_(user_ids)).values(parent_event_id=None),
        delete(Event).where(Event.user_id.in_(user_ids)),
        delete(ImportantDateRange).where(ImportantDateRange.user_id.in_(user_ids)),
        delete(Location).where(Location.user_id.in_(user_ids)),
        delete(AssistantMessage).where(AssistantMessage.session_id.in_(owned_sessions)),
        delete(PendingProposal).where(PendingProposal.session_id.in_(owned_sessions)),
        delete(AssistantTurnLog).where(AssistantTurnLog.session_id.in_(owned_sessions)),
        delete(AssistantSession).where(AssistantSession.user_id.in_(user_ids)),
        delete(ActionHistory).where(ActionHistory.user_id.in_(user_ids)),
        delete(DailyActualLog).where(DailyActualLog.user_id.in_(user_ids)),
        delete(SleepLog).where(SleepLog.user_id.in_(user_ids)),
        delete(PersonaConversation).where(PersonaConversation.user_id.in_(user_ids)),
        delete(PointsLedger).where(PointsLedger.user_id.in_(user_ids)),
        delete(UserSession).where(UserSession.user_id.in_(user_ids)),
        update(LlmUsageLog).where(LlmUsageLog.user_id.in_(user_ids)).values(user_id=None, is_demo=True),
        delete(User).where(User.id.in_(user_ids), User.is_demo.is_(True)),
    ]
    for statement in statements:
        db.execute(statement.execution_options(synchronize_session=False))


def purge_expired_demo_users(db: Session, now: datetime | None = None) -> int:
    """Deletes every demo account whose 24 hours are over. Returns how many were deleted."""
    now = now or utc_now_naive()
    user_ids = list(
        db.execute(select(User.id).where(User.is_demo.is_(True), User.demo_expires_at <= now)).scalars()
    )
    if not user_ids:
        return 0
    purge_demo_users(db, user_ids)
    db.commit()
    db.expire_all()
    logger.info("[demo] deleted %d expired demo accounts", len(user_ids))
    return len(user_ids)


def run_demo_cleanup_job() -> None:
    with SessionLocal() as db:
        try:
            purge_expired_demo_users(db)
        except Exception:
            db.rollback()
            logger.exception("[demo] cleanup failed")


def register_demo_cleanup_job() -> None:
    """Hourly, whether or not demo mode is on, so accounts left from before it was turned off still go away."""
    scheduler.add_job(
        run_demo_cleanup_job,
        trigger="interval",
        hours=1,
        id=CLEANUP_JOB_ID,
        replace_existing=True,
        coalesce=True,
    )
