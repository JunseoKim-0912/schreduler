from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CONFLICT, NOT_FOUND
from app.models.important_date_range import ImportantDateRange
from app.schemas.date_range import DateRangeCreate, DateRangeRead, DateRangeUpdate
from app.services import date_range_service

router = APIRouter(prefix="/date-ranges", tags=["date-ranges"])


@router.post("", response_model=DateRangeRead, status_code=status.HTTP_201_CREATED, summary="중요 기간 등록", responses=NOT_FOUND)
def create_date_range(
    data: DateRangeCreate, db: Session = Depends(get_db)
) -> ImportantDateRange:
    """반복 일정의 종료 기준으로 재사용할 기간을 등록한다 (예: '2026 가을학기')."""
    return date_range_service.create_date_range(db, data)


@router.get("", response_model=list[DateRangeRead], summary="중요 기간 목록")
def list_date_ranges(
    user_id: int | None = Query(default=None, description="이 사용자의 것만 조회"),
    db: Session = Depends(get_db),
) -> list[ImportantDateRange]:
    """`user_id`로 특정 사용자의 기간만 거를 수 있다."""
    return date_range_service.list_date_ranges(db, user_id=user_id)


@router.get("/{date_range_id}", response_model=DateRangeRead, summary="중요 기간 조회", responses=NOT_FOUND)
def get_date_range(date_range_id: int, db: Session = Depends(get_db)) -> ImportantDateRange:
    """기간 하나를 조회한다."""
    date_range = date_range_service.get_date_range(db, date_range_id)
    if date_range is None:
        raise NotFoundError("Date range not found")
    return date_range


@router.put("/{date_range_id}", response_model=DateRangeRead, summary="중요 기간 수정", responses=NOT_FOUND)
def update_date_range(
    date_range_id: int, data: DateRangeUpdate, db: Session = Depends(get_db)
) -> ImportantDateRange:
    """보낸 필드만 수정한다. 종료일이 시작일보다 빠르면 422."""
    date_range = date_range_service.update_date_range(db, date_range_id, data)
    if date_range is None:
        raise NotFoundError("Date range not found")
    return date_range


@router.delete("/{date_range_id}", status_code=status.HTTP_204_NO_CONTENT, summary="중요 기간 삭제", responses={**NOT_FOUND, **CONFLICT})
def delete_date_range(date_range_id: int, db: Session = Depends(get_db)) -> None:
    """기간을 삭제한다. 이 기간을 반복 종료 기준으로 쓰는 일정이 있으면 409."""
    deleted = date_range_service.delete_date_range(db, date_range_id)
    if not deleted:
        raise NotFoundError("Date range not found")
