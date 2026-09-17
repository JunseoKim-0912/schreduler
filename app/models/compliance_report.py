from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import NonComplianceCategory

if TYPE_CHECKING:
    from app.models.event_instance import EventInstance


class ComplianceReport(Base):
    """FR-6 미준수 사유 기록. reason_category 버튼 선택만으로 끝나면 llm_triggered=False로
    저장되고(FR-6 LLM 우회 UI 숏컷), other 선택이나 자유 텍스트 입력 시에만 True가 된다."""

    __tablename__ = "compliance_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_instance_id: Mapped[int] = mapped_column(ForeignKey("event_instances.id"))
    reason_category: Mapped[NonComplianceCategory] = mapped_column(
        Enum(NonComplianceCategory, native_enum=False, name="non_compliance_category")
    )
    reason_text: Mapped[str | None] = mapped_column(String(150), nullable=True)
    llm_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    event_instance: Mapped["EventInstance"] = relationship(back_populates="compliance_reports")
