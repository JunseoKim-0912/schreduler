from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import ActionSource, ActionType

if TYPE_CHECKING:
    from app.models.user import User


class ActionHistory(Base):
    """FR-2 v3.6 되돌리기용 변경 기록. 여러 개를 한꺼번에 바꾼 요청도 한 행으로 묶는다.

    snapshot_before에는 변경 직전의 관련 행 전체(events / event_instances / compliance_reports)를 컬럼 값
    그대로 담는다. undone_at이 채워지면 이미 되돌린 기록이다.
    """

    __tablename__ = "action_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action_type: Mapped[ActionType] = mapped_column(Enum(ActionType, native_enum=False, name="action_type"))
    source: Mapped[ActionSource] = mapped_column(Enum(ActionSource, native_enum=False, name="action_source"))
    summary_text: Mapped[str] = mapped_column(String(500))
    snapshot_before: Mapped[dict[str, Any]] = mapped_column(JSON)
    affected_ids: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship()
