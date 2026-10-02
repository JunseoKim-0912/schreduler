from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.openapi import CONFLICT, NOT_FOUND
from app.models.location import Location
from app.models.user import User
from app.schemas.location import LocationCreate, LocationRead, LocationUpdate
from app.services import location_service

router = APIRouter(prefix="/locations", tags=["locations"])


@router.post("", response_model=LocationRead, status_code=status.HTTP_201_CREATED, summary="장소 등록")
def create_location(data: LocationCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Location:
    """장소와 기본 이동시간(분)을 등록한다. 이 장소의 일정에는 이동시간 하위 일정이 자동으로 붙는다 (FR-5)."""
    return location_service.create_location(db, user, data)


@router.get("", response_model=list[LocationRead], summary="장소 목록")
def list_locations(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Location]:
    """내 장소 전부."""
    return location_service.list_locations(db, user)


@router.get("/{location_id}", response_model=LocationRead, summary="장소 조회", responses=NOT_FOUND)
def get_location(location_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Location:
    """장소 하나를 조회한다. 다른 사용자의 장소는 404."""
    return location_service.get_location(db, user, location_id)


@router.put("/{location_id}", response_model=LocationRead, summary="장소 수정", responses=NOT_FOUND)
def update_location(
    location_id: int, data: LocationUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Location:
    """보낸 필드만 수정한다."""
    return location_service.update_location(db, location_service.get_location(db, user, location_id), data)


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT, summary="장소 삭제", responses={**NOT_FOUND, **CONFLICT})
def delete_location(location_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    """장소를 삭제한다. 이 장소를 쓰는 일정이 있으면 409."""
    location_service.delete_location(db, location_service.get_location(db, user, location_id))
