from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.exceptions import NotFoundError
from app.core.openapi import CONFLICT, NOT_FOUND
from app.models.location import Location
from app.schemas.location import LocationCreate, LocationRead, LocationUpdate
from app.services import location_service

router = APIRouter(prefix="/locations", tags=["locations"])


@router.post("", response_model=LocationRead, status_code=status.HTTP_201_CREATED, summary="장소 등록", responses=NOT_FOUND)
def create_location(data: LocationCreate, db: Session = Depends(get_db)) -> Location:
    """장소와 기본 이동시간(분)을 등록한다. 이 장소의 일정에는 이동시간 하위 일정이 자동으로 붙는다 (FR-5)."""
    return location_service.create_location(db, data)


@router.get("", response_model=list[LocationRead], summary="장소 목록")
def list_locations(
    user_id: int | None = Query(default=None, description="이 사용자의 것만 조회"),
    db: Session = Depends(get_db),
) -> list[Location]:
    """`user_id`로 특정 사용자의 장소만 거를 수 있다."""
    return location_service.list_locations(db, user_id=user_id)


@router.get("/{location_id}", response_model=LocationRead, summary="장소 조회", responses=NOT_FOUND)
def get_location(location_id: int, db: Session = Depends(get_db)) -> Location:
    """장소 하나를 조회한다."""
    location = location_service.get_location(db, location_id)
    if location is None:
        raise NotFoundError("Location not found")
    return location


@router.put("/{location_id}", response_model=LocationRead, summary="장소 수정", responses=NOT_FOUND)
def update_location(
    location_id: int, data: LocationUpdate, db: Session = Depends(get_db)
) -> Location:
    """보낸 필드만 수정한다."""
    location = location_service.update_location(db, location_id, data)
    if location is None:
        raise NotFoundError("Location not found")
    return location


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT, summary="장소 삭제", responses={**NOT_FOUND, **CONFLICT})
def delete_location(location_id: int, db: Session = Depends(get_db)) -> None:
    """장소를 삭제한다. 이 장소를 쓰는 일정이 있으면 409."""
    deleted = location_service.delete_location(db, location_id)
    if not deleted:
        raise NotFoundError("Location not found")
