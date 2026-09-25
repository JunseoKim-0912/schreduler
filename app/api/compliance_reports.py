from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.openapi import CURRENT_USER, LLM_ERRORS, NOT_FOUND
from app.core.i18n import get_response_language
from app.i18n import Language, non_compliance_category_label
from app.schemas.compliance_report import (
    ComplianceReportCreate,
    ComplianceReportRead,
    ComplianceReportStatsResponse,
    NonComplianceCategoryRead,
)
from app.services import compliance_report_service

router = APIRouter(prefix="/compliance-reports", tags=["compliance-reports"])


@router.post("", response_model=ComplianceReportRead, status_code=status.HTTP_201_CREATED, summary="미준수 사유 기록", responses={**NOT_FOUND, **LLM_ERRORS})
def create_compliance_report(
    data: ComplianceReportCreate, db: Session = Depends(get_db)
) -> ComplianceReportRead:
    """FR-6: 미준수 사유를 기록한다.

    reason_category만(other 아님) 선택하고 reason_text 없이 끝나면 LLM을
    호출하지 않고 바로 저장한다 (llm_triggered=False). other를 선택했거나
    reason_text를 채웠다면 LLM으로 공감 피드백을 생성해 함께 반환한다
    (llm_triggered=True).
    """
    report, feedback = compliance_report_service.create_compliance_report(db, data)

    return ComplianceReportRead(
        id=report.id,
        event_instance_id=report.event_instance_id,
        reason_category=report.reason_category,
        reason_category_label=non_compliance_category_label(
            report.reason_category, report.event_instance.event.user.preferred_language
        ),
        reason_text=report.reason_text,
        llm_triggered=report.llm_triggered,
        created_at=report.created_at,
        llm_feedback=feedback,
    )


@router.get("/categories", response_model=list[NonComplianceCategoryRead], summary="미준수 카테고리 목록 (버튼용)", responses=CURRENT_USER)
def list_non_compliance_categories(
    language: Language = Depends(get_response_language),
) -> list[NonComplianceCategoryRead]:
    """FR-6 카테고리 버튼 목록. code는 POST의 reason_category로 그대로 보내고, label만 화면에 보여준다."""
    return compliance_report_service.list_non_compliance_categories(language)


# 정적 경로라서 상관없지만, 나중에 GET /compliance-reports/{report_id}를 추가하면
# 이 라우트가 그보다 먼저 등록돼 있어야 "stats"가 report_id로 잘못 파싱되지 않는다.
@router.get("/stats", response_model=ComplianceReportStatsResponse, summary="미준수 사유 통계")
def get_compliance_report_stats(
    days: int = Query(default=30, gt=0, description="최근 며칠간을 집계할지"),
    user_id: int | None = Query(default=None, description="이 사용자의 리포트만 집계 (라벨도 이 사용자의 언어로)"),
    db: Session = Depends(get_db),
) -> ComplianceReportStatsResponse:
    """최근 days일(기본 30일)간 reason_category별 ComplianceReport 분포를 반환한다."""
    return compliance_report_service.get_compliance_report_stats(db, days=days, user_id=user_id)
