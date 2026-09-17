from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import NonComplianceCategory


class ComplianceReportCreate(BaseModel):
    event_instance_id: int
    reason_category: NonComplianceCategory
    reason_text: str | None = Field(default=None, max_length=150)


class ComplianceReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_instance_id: int
    reason_category: NonComplianceCategory
    reason_text: str | None
    llm_triggered: bool
    created_at: datetime
    # LLM이 호출됐을 때만(llm_triggered=True) 값이 있다. DB 컬럼이 아니라
    # 이번 요청에서 생성된 결과를 그대로 응답에 실어 보내는 필드다.
    llm_feedback: str | None = None


class ComplianceReportCategoryStat(BaseModel):
    reason_category: NonComplianceCategory
    count: int


class ComplianceReportStatsResponse(BaseModel):
    since: datetime
    until: datetime
    total: int
    by_category: list[ComplianceReportCategoryStat]
