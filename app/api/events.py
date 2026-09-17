from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.event import Event
from app.schemas.event import EventCreate, EventRead, EventUpdate
from app.schemas.event_parse import EventParseRequest, EventParseResponse
from app.services import event_parse_service, event_service
from app.services.llm_client import (
    LLMClientError,
    LLMConfigError,
    LLMRequestError,
    LLMResponseParsingError,
)

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventRead, status_code=status.HTTP_201_CREATED)
def create_event(data: EventCreate, db: Session = Depends(get_db)) -> Event:
    try:
        return event_service.create_event(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/parse", response_model=EventParseResponse)
def parse_event(data: EventParseRequest, db: Session = Depends(get_db)) -> EventParseResponse:
    """FR-2: 자연어 발화 한 턴을 슬롯필링한다. session_id를 생략하면 새 대화를
    시작하고, 이전 응답의 session_id를 그대로 보내면 대화를 이어간다."""
    try:
        return event_parse_service.parse_event_utterance(db, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LLMConfigError as exc:
        # 서버 설정(.env의 LLM_API_KEY 등) 문제 — 요청 자체의 잘못이 아니다.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    except LLMResponseParsingError as exc:
        # LLM이 응답은 했지만 우리가 기대한 스키마와 다름 — upstream이 죽은 게
        # 아니므로 502가 아니라 422로 다룬다.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except LLMRequestError as exc:
        # LLM API 호출 자체(네트워크/4xx/5xx)가 실패함 — 진짜 upstream 문제.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except LLMClientError as exc:
        # 위에서 못 잡은 나머지 LLMClientError에 대한 안전망.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("", response_model=list[EventRead])
def list_events(
    user_id: int | None = Query(default=None), db: Session = Depends(get_db)
) -> list[Event]:
    return event_service.list_events(db, user_id=user_id)


@router.get("/{event_id}", response_model=EventRead)
def get_event(event_id: int, db: Session = Depends(get_db)) -> Event:
    event = event_service.get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event


@router.put("/{event_id}", response_model=EventRead)
def update_event(event_id: int, data: EventUpdate, db: Session = Depends(get_db)) -> Event:
    try:
        event = event_service.update_event(db, event_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return event


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event_id: int, db: Session = Depends(get_db)) -> None:
    deleted = event_service.delete_event(db, event_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
