from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import EngagementScope, EscalationStage

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.user import User


class EngagementState(Base):
    """FR-4-1 통합 에스컬레이션 정책의 상태. scope가 EVENT면 ref_event_id로 어떤
    이벤트에 대한 무응답인지 가리키고, GLOBAL이면 앱 전체 미접속을 추적한다."""

    __tablename__ = "engagement_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    scope: Mapped[EngagementScope] = mapped_column(
        Enum(EngagementScope, native_enum=False, name="engagement_scope")
    )
    ref_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    last_response_at: Mapped[datetime | None] = mapped_column(nullable=True)
    escalation_stage: Mapped[EscalationStage] = mapped_column(
        Enum(EscalationStage, native_enum=False, name="escalation_stage"),
        default=EscalationStage.NORMAL,
    )
    stage_updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="engagement_states")
    ref_event: Mapped["Event | None"] = relationship()
