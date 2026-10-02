from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.openapi import CONFLICT, NOT_FOUND
from app.models.enums import ActionSource
from app.models.important_date_range import ImportantDateRange
from app.models.user import User
from app.schemas.date_range import DateRangeCreate, DateRangeRead, DateRangeUpdate, DateRangeUsageRead
from app.services import date_range_command_service, date_range_service

router = APIRouter(prefix="/date-ranges", tags=["date-ranges"])


@router.post("", response_model=DateRangeRead, status_code=status.HTTP_201_CREATED, summary="중요 기간 등록")
def create_date_range(
    data: DateRangeCreate, response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> ImportantDateRange:
    """반복 일정의 종료 기준으로 재사용할 기간을 등록한다 (예: '2026 가을학기'). 되돌리기 id는 헤더 `X-Action-Id`."""
    date_range, action = date_range_command_service.create_range(
        db, user, data.name, data.start_date, data.end_date, ActionSource.UI
    )
    response.headers["X-Action-Id"] = str(action.id)
    return date_range


@router.get("", response_model=list[DateRangeUsageRead], summary="중요 기간 목록")
def list_date_ranges(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[DateRangeUsageRead]:
    """내 기간 전부. `event_count`는 이 기간을 반복 기준으로 쓰는 일정 수(하위 일정 제외)."""
    return date_range_service.list_date_ranges_with_usage(db, user)


@router.get("/{date_range_id}", response_model=DateRangeRead, summary="중요 기간 조회", responses=NOT_FOUND)
def get_date_range(date_range_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ImportantDateRange:
    """기간 하나를 조회한다. 다른 사용자의 기간은 404."""
    return date_range_service.get_date_range(db, user, date_range_id)


@router.put("/{date_range_id}", response_model=DateRangeRead, summary="중요 기간 수정", responses=NOT_FOUND)
def update_date_range(
    date_range_id: int,
    data: DateRangeUpdate,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ImportantDateRange:
    """보낸 필드만 수정한다. 종료일이 시작일보다 빠르면 422.

    이 기간을 쓰는 반복 일정의 회차를 다시 맞춘다: 늘어난 날짜의 회차를 만들고, 범위 밖의 대기 회차는 취소한다
    (완료·놓침 기록은 그대로). 되돌리기 id는 헤더 `X-Action-Id`.
    """
    date_range = date_range_service.get_date_range(db, user, date_range_id)
    changes = data.model_dump(exclude_unset=True)
    result = date_range_command_service.update_range(
        db,
        user,
        date_range,
        name=changes.get("name"),
        start=changes.get("start_date"),
        end=changes.get("end_date"),
        source=ActionSource.UI,
    )
    response.headers["X-Action-Id"] = str(result.action.id)
    return result.date_range


@router.delete(
    "/{date_range_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="중요 기간 삭제",
    responses={**NOT_FOUND, **CONFLICT},
)
def delete_date_range(
    date_range_id: int,
    response: Response,
    mode: Literal["range_only", "with_events"] | None = Query(
        default=None,
        description="이 기간을 쓰는 일정이 있을 때의 처리: range_only(기간만 지우고 일정은 이미 만들어진 마지막 회차에서 끝남) "
        "/ with_events(일정도 함께 삭제). 쓰는 일정이 있는데 빠지면 409",
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """기간을 삭제한다. 되돌리기 id는 헤더 `X-Action-Id`."""
    date_range = date_range_service.get_date_range(db, user, date_range_id)
    action = date_range_command_service.delete_range(db, user, date_range, mode, ActionSource.UI)
    response.headers["X-Action-Id"] = str(action.id)
