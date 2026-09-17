from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.schemas.compliance_report import (
    ComplianceReportCreate,
    ComplianceReportRead,
    ComplianceReportStatsResponse,
)
from app.services import compliance_report_service
from app.services.llm_client import (
    LLMClientError,
    LLMConfigError,
    LLMRequestError,
    LLMResponseParsingError,
)

router = APIRouter(prefix="/compliance-reports", tags=["compliance-reports"])


@router.post("", response_model=ComplianceReportRead, status_code=status.HTTP_201_CREATED)
def create_compliance_report(
    data: ComplianceReportCreate, db: Session = Depends(get_db)
) -> ComplianceReportRead:
    """FR-6: 미준수 사유를 기록한다.

    reason_category만(other 아님) 선택하고 reason_text 없이 끝나면 LLM을
    호출하지 않고 바로 저장한다 (llm_triggered=False). other를 선택했거나
    reason_text를 채웠다면 LLM으로 공감 피드백을 생성해 함께 반환한다
    (llm_triggered=True).
    """
    try:
        report, feedback = compliance_report_service.create_compliance_report(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LLMConfigError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except LLMResponseParsingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except LLMRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except LLMClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return ComplianceReportRead(
        id=report.id,
        event_instance_id=report.event_instance_id,
        reason_category=report.reason_category,
        reason_text=report.reason_text,
        llm_triggered=report.llm_triggered,
        created_at=report.created_at,
        llm_feedback=feedback,
    )


# 정적 경로라서 상관없지만, 나중에 GET /compliance-reports/{report_id}를 추가하면
# 이 라우트가 그보다 먼저 등록돼 있어야 "stats"가 report_id로 잘못 파싱되지 않는다.
@router.get("/stats", response_model=ComplianceReportStatsResponse)
def get_compliance_report_stats(
    days: int = Query(default=30, gt=0),
    user_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ComplianceReportStatsResponse:
    """최근 days일(기본 30일)간 reason_category별 ComplianceReport 분포를 반환한다."""
    return compliance_report_service.get_compliance_report_stats(db, days=days, user_id=user_id)
