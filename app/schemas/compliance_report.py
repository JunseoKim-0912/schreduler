from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import NonComplianceCategory


class ComplianceReportCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event_instance_id": 1,
                    "reason_category": "overslept"
                },
                {
                    "event_instance_id": 1,
                    "reason_category": "other",
                    "reason_text": "버스가 20분이나 안 왔어요"
                }
            ]
        },
    )

    event_instance_id: int
    reason_category: NonComplianceCategory
    reason_text: str | None = Field(default=None, max_length=150)


class ComplianceReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_instance_id: int
    reason_category: NonComplianceCategory
    reason_category_label: str  # 이벤트 소유자의 preferred_language로 고른 화면 표시 라벨
    reason_text: str | None
    llm_triggered: bool
    created_at: datetime
    # LLM이 호출됐을 때만(llm_triggered=True) 값이 있다. DB 컬럼이 아니라
    # 이번 요청에서 생성된 결과를 그대로 응답에 실어 보내는 필드다.
    llm_feedback: str | None = None


class NonComplianceCategoryRead(BaseModel):
    code: NonComplianceCategory
    label: str


class ComplianceReportCategoryStat(BaseModel):
    reason_category: NonComplianceCategory
    label: str
    count: int


class ComplianceReportStatsResponse(BaseModel):
    since: datetime
    until: datetime
    total: int
    by_category: list[ComplianceReportCategoryStat]
