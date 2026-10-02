from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LlmUsageLog(Base):
    """One row per LLM API call, written by app.services.llm_usage. The daily budget sums cost_usd from here."""

    __tablename__ = "llm_usage_logs"
    __table_args__ = (Index("ix_llm_usage_logs_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # None for developer scripts that call the API without a user (compare_prompt_cache), and for demo accounts that
    # have been deleted — their rows stay so the day's demo total and the cost reports keep what was actually paid.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Demo calls are summed against the DEMO_* caps, never against LLM_DAILY_BUDGET_TOTAL_USD.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    feature: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Naive UTC; the day boundary (APP_TIMEZONE midnight) is converted to UTC when summing.
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
