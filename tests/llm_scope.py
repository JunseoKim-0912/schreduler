"""For tests that call the LLM client directly: every call must run inside llm_usage.usage_scope().

    from tests.llm_scope import llm_scope  # noqa: F401  (autouse fixture)
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, LlmUsageLog
from app.services import llm_usage


@pytest.fixture(autouse=True)
def llm_scope() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db, llm_usage.usage_scope(db, None, "script"):
        yield db
    engine.dispose()


def usage_rows(db: Session) -> list[LlmUsageLog]:
    return list(db.execute(select(LlmUsageLog).order_by(LlmUsageLog.id)).scalars())
