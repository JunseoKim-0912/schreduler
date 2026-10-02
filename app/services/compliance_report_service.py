from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now_naive
from app.core.exceptions import ConflictError, NotFoundError
from app.models.compliance_report import ComplianceReport
from app.models.enums import EventInstanceStatus, NonComplianceCategory
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.schemas.compliance_report import (
    ComplianceReportCategoryStat,
    ComplianceReportCreate,
    ComplianceReportStatsResponse,
    NonComplianceCategoryRead,
)
from app.schemas.persona import PersonaRead
from app.i18n import non_compliance_category_label
from app.models.user import User
from app.services import llm_usage
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
    user: User,
    data: ComplianceReportCreate,
    *,
    http_client: httpx.Client | None = None,
) -> tuple[ComplianceReport, str | None]:
    """ComplianceReport를 저장하고, LLM이 트리거된 경우 그 공감 피드백 문장을
    함께 반환한다 (피드백 자체는 DB에 저장하지 않는다).
    """
    event_instance = db.get(EventInstance, data.event_instance_id)
    if event_instance is None or event_instance.event.user_id != user.id:
        raise NotFoundError(f"event_instance_id {data.event_instance_id} does not exist")
    if event_instance.status == EventInstanceStatus.CANCELLED:
        raise ConflictError(f"event_instance_id {data.event_instance_id} is cancelled")

    llm_triggered = _should_trigger_llm(data)

    with llm_usage.usage_scope(db, user.id, "compliance"):
        feedback: str | None = None
        if llm_triggered:
            persona = PersonaRead.model_validate(user.selected_persona) if user.selected_persona else None
            feedback = generate_compliance_feedback(
                data.reason_category,
                data.reason_text,
                persona=persona,
                language=user.preferred_language,
                http_client=http_client,
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


def get_compliance_report_stats(db: Session, user: User, *, days: int = 30) -> ComplianceReportStatsResponse:
    """최근 days일간 이 사용자 일정의 ComplianceReport를 reason_category별로 집계한다 (event_instance -> event 조인).
    한 건도 없는 카테고리도 count=0으로 항상 포함해서, 클라이언트가 "이 카테고리는 응답에 아예 없음"을 따로 처리할
    필요가 없게 한다. 라벨은 이 사용자의 언어다.
    """
    until = utc_now_naive()
    since = until - timedelta(days=days)

    stmt = select(ComplianceReport.reason_category, func.count(ComplianceReport.id)).where(
        ComplianceReport.created_at >= since,
        ComplianceReport.created_at <= until,
    )
    stmt = (
        stmt.join(EventInstance, ComplianceReport.event_instance_id == EventInstance.id)
        .join(Event, EventInstance.event_id == Event.id)
        .where(Event.user_id == user.id)
    )
    stmt = stmt.group_by(ComplianceReport.reason_category)

    counts: dict[NonComplianceCategory, int] = dict.fromkeys(NonComplianceCategory, 0)
    for category, count in db.execute(stmt).all():
        counts[category] = count

    language = user.preferred_language
    by_category = [
        ComplianceReportCategoryStat(
            reason_category=category,
            label=non_compliance_category_label(category, language),
            count=count,
        )
        for category, count in counts.items()
    ]
    return ComplianceReportStatsResponse(
        since=since,
        until=until,
        total=sum(counts.values()),
        by_category=by_category,
    )



def list_non_compliance_categories(language: str) -> list[NonComplianceCategoryRead]:
    """FR-6 UI 숏컷 버튼용 카테고리 목록 (enum 선언 순서)."""
    return [
        NonComplianceCategoryRead(code=category, label=non_compliance_category_label(category, language))
        for category in NonComplianceCategory
    ]
