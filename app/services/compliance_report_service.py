from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.compliance_report import ComplianceReport
from app.models.enums import NonComplianceCategory
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.schemas.compliance_report import (
    ComplianceReportCategoryStat,
    ComplianceReportCreate,
    ComplianceReportStatsResponse,
)
from app.services.llm_client import generate_compliance_feedback


def _should_trigger_llm(data: ComplianceReportCreate) -> bool:
    """FR-6 "LLM 우회 UI 숏컷": 카테고리 버튼 선택만으로 끝나면(other가 아니고
    자유 텍스트도 없으면) LLM을 호출하지 않는다. other를 선택했거나 자유
    텍스트를 추가로 입력한 경우에만 LLM을 호출해 공감 피드백을 생성한다.
    """
    if data.reason_category == NonComplianceCategory.OTHER:
        return True
    return bool(data.reason_text and data.reason_text.strip())


def create_compliance_report(
    db: Session,
    data: ComplianceReportCreate,
    *,
    http_client: httpx.Client | None = None,
) -> tuple[ComplianceReport, str | None]:
    """ComplianceReport를 저장하고, LLM이 트리거된 경우 그 공감 피드백 문장을
    함께 반환한다 (피드백 자체는 DB에 저장하지 않는다).
    """
    if db.get(EventInstance, data.event_instance_id) is None:
        raise ValueError(f"event_instance_id {data.event_instance_id} does not exist")

    llm_triggered = _should_trigger_llm(data)

    feedback: str | None = None
    if llm_triggered:
        feedback = generate_compliance_feedback(
            data.reason_category, data.reason_text, http_client=http_client
        )

    report = ComplianceReport(
        event_instance_id=data.event_instance_id,
        reason_category=data.reason_category,
        reason_text=data.reason_text,
        llm_triggered=llm_triggered,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    return report, feedback


def get_compliance_report_stats(
    db: Session,
    *,
    days: int = 30,
    user_id: int | None = None,
) -> ComplianceReportStatsResponse:
    """최근 days일간 ComplianceReport의 reason_category별 분포를 집계한다.

    user_id를 주면 그 사용자 소유 이벤트에 대한 리포트만 집계한다 (event_instance
    -> event 조인). 한 건도 없는 카테고리도 count=0으로 항상 포함해서, 클라이언트가
    "이 카테고리는 응답에 아예 없음"을 따로 처리할 필요가 없게 한다.
    """
    until = datetime.utcnow()
    since = until - timedelta(days=days)

    stmt = select(ComplianceReport.reason_category, func.count(ComplianceReport.id)).where(
        ComplianceReport.created_at >= since,
        ComplianceReport.created_at <= until,
    )
    if user_id is not None:
        stmt = (
            stmt.join(EventInstance, ComplianceReport.event_instance_id == EventInstance.id)
            .join(Event, EventInstance.event_id == Event.id)
            .where(Event.user_id == user_id)
        )
    stmt = stmt.group_by(ComplianceReport.reason_category)

    counts: dict[NonComplianceCategory, int] = dict.fromkeys(NonComplianceCategory, 0)
    for category, count in db.execute(stmt).all():
        counts[category] = count

    by_category = [
        ComplianceReportCategoryStat(reason_category=category, count=count)
        for category, count in counts.items()
    ]
    return ComplianceReportStatsResponse(
        since=since,
        until=until,
        total=sum(counts.values()),
        by_category=by_category,
    )
