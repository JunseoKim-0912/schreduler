"""LLM cost by day, user and feature from llm_usage_logs (days follow APP_TIMEZONE).

    python -m app.scripts.usage_report --days 7
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import app_timezone, local_today
from app.core.config import settings
from app.core.db import SessionLocal
from app.models.llm_usage_log import LlmUsageLog
from app.models.user import User
from app.services.llm_usage import day_window


@dataclass
class UsageRow:
    day: date
    user: str
    feature: str
    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def collect(db: Session, days: int, today: date | None = None) -> list[UsageRow]:
    today = today or local_today()
    first = today - timedelta(days=days - 1)
    start = day_window(_midnight(first)).start_utc
    end = day_window(_midnight(today)).end_utc
    names = dict(db.execute(select(User.id, User.name)).tuples().all())
    rows: dict[tuple[date, str, str], UsageRow] = {}
    logs = db.execute(
        select(LlmUsageLog).where(LlmUsageLog.created_at >= start, LlmUsageLog.created_at < end).order_by(LlmUsageLog.created_at)
    ).scalars()
    for log in logs:
        day = log.created_at.replace(tzinfo=UTC).astimezone(app_timezone()).date()
        user = f"{log.user_id} {names.get(log.user_id, '?')}" if log.user_id is not None else "(script)"
        row = rows.setdefault((day, user, log.feature), UsageRow(day, user, log.feature))
        row.calls += 1
        row.input_tokens += log.input_tokens
        row.cached_tokens += log.cached_tokens
        row.output_tokens += log.output_tokens
        row.cost_usd += log.cost_usd
    return sorted(rows.values(), key=lambda r: (r.day, r.user, r.feature))


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=app_timezone())


def render(rows: list[UsageRow], days: int) -> str:
    header = ("date", "user", "feature", "calls", "input", "cached", "output", "cost_usd")
    table = [header] + [
        (r.day.isoformat(), r.user, r.feature, str(r.calls), str(r.input_tokens), str(r.cached_tokens), str(r.output_tokens), f"{r.cost_usd:.4f}")
        for r in rows
    ]
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    numeric = range(3, len(header))
    lines = [
        "  ".join(cell.rjust(widths[i]) if i in numeric else cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip()
        for line in table
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))

    per_day: dict[date, float] = defaultdict(float)
    for r in rows:
        per_day[r.day] += r.cost_usd
    lines.append("")
    lines.append(f"Daily totals (cap: {settings.llm_daily_budget_total_usd:.2f} USD overall, {settings.llm_daily_budget_per_user_usd:.2f} USD per user)")
    lines.extend(f"  {day.isoformat()}  {cost:.4f}" for day, cost in sorted(per_day.items()))
    lines.append(f"Total over {days} day(s): {sum(per_day.values()):.4f} USD")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="how many days back, including today")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    with SessionLocal() as db:
        rows = collect(db, args.days)
    if not rows:
        print(f"No LLM usage in the last {args.days} day(s) ({settings.app_timezone}).")
        return 0
    print(render(rows, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
