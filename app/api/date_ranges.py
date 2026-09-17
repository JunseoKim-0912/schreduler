from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.important_date_range import ImportantDateRange
from app.schemas.date_range import DateRangeCreate, DateRangeRead, DateRangeUpdate
from app.services import date_range_service

router = APIRouter(prefix="/date-ranges", tags=["date-ranges"])


@router.post("", response_model=DateRangeRead, status_code=status.HTTP_201_CREATED)
def create_date_range(
    data: DateRangeCreate, db: Session = Depends(get_db)
) -> ImportantDateRange:
    try:
        return date_range_service.create_date_range(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("", response_model=list[DateRangeRead])
def list_date_ranges(
    user_id: int | None = Query(default=None), db: Session = Depends(get_db)
) -> list[ImportantDateRange]:
    return date_range_service.list_date_ranges(db, user_id=user_id)


@router.get("/{date_range_id}", response_model=DateRangeRead)
def get_date_range(date_range_id: int, db: Session = Depends(get_db)) -> ImportantDateRange:
    date_range = date_range_service.get_date_range(db, date_range_id)
    if date_range is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Date range not found")
    return date_range


@router.put("/{date_range_id}", response_model=DateRangeRead)
def update_date_range(
    date_range_id: int, data: DateRangeUpdate, db: Session = Depends(get_db)
) -> ImportantDateRange:
    try:
        date_range = date_range_service.update_date_range(db, date_range_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if date_range is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Date range not found")
    return date_range


@router.delete("/{date_range_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_date_range(date_range_id: int, db: Session = Depends(get_db)) -> None:
    deleted = date_range_service.delete_date_range(db, date_range_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Date range not found")
