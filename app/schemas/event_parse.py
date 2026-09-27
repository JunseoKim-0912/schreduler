from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.enums import EventType, Importance
from app.schemas.common import NonEmptyStr
from app.schemas.event_command import CommandResult
from app.services.llm_client import ClarifyingQuestion, Intent, QuestionSlot


class EventParseRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "utterance": "매주 화요일 저녁 7시에 알고리즘 스터디"
                }
            ]
        },
    )

    user_id: int
    utterance: NonEmptyStr
    session_id: str | None = None


class NewDateRangeDraft(BaseModel):
    """초안과 함께 확정할 때 새로 만들 반복 기간. 같은 이름의 기간이 그사이 생겼으면 그것을 쓴다."""

    name: str
    start_date: date
    end_date: date
    auto_named: bool = False  # 사용자가 이름을 말하지 않아 앱이 붙인 이름


class NewLocationDraft(BaseModel):
    """초안과 함께 확정할 때 새로 등록할 장소. 같은 이름의 장소가 그사이 생겼으면 그것을 쓴다."""

    name: str
    default_travel_minutes: int


class EventDraft(BaseModel):
    """모든 슬롯이 채워졌을 때 반환하는 완성된 이벤트 초안.

    EventCreate와 같은 모양이라 이 draft를 그대로 POST /events에 넘기면 이벤트가
    바로 생성된다 (아직 이 draft 자체가 DB에 저장된 상태는 아니다 — 클라이언트가
    확인 후 실제로 POST해야 한다). 단발 일정은 is_recurring=false, recurrence_rule·date_range_id가
    null이고 start_time/end_time은 그 날짜의 일시다. 반복 일정은 첫 날짜(말하지 않았으면 반복 기간의
    start_date, 없으면 오늘)에 HH:MM을 합친 일시이고, recurrence_rule은 frequency/by_day로 조립한 RRULE이다.
    """

    user_id: int
    title: str
    event_type: EventType = EventType.SCHEDULED
    start_time: datetime | None  # deadline이면 null이고 end_time이 마감 일시다
    end_time: datetime
    importance: Importance | None
    is_recurring: bool = True
    recurrence_rule: str | None = None
    date_range_id: int | None
    # 등록된 기간 대신 새 반복 기간을 쓸 때. 확인(POST /events/commands/confirm)하면 이벤트와 같은 트랜잭션에서 만든다.
    new_date_range: NewDateRangeDraft | None = None
    location_id: int | None = None
    location_name: str | None = None  # 화면 표시용 (POST /events는 무시한다)
    # 등록되지 않은 장소를 말했을 때. 확인하면 이벤트와 같은 트랜잭션에서 등록하고 이동시간 하위 일정이 생긴다.
    new_location: NewLocationDraft | None = None


# 확인 카드에서 고친 항목 (카드의 행 단위). 바로 앞 초안과 비교한다.
DraftField = Literal["title", "event_type", "date", "time", "importance", "recurrence", "location"]


class EventParseResponse(BaseModel):
    session_id: str
    is_complete: bool
    next_question: ClarifyingQuestion | None = None
    missing_slots: list[QuestionSlot] = []
    draft: EventDraft | None = None
    # v3.6: 발화 의도. create면 위 필드들(기존 슬롯필링)을, delete/update면 command를 본다.
    intent: Intent = "create"
    # 사용자 언어로 된 안내 문구 (의도 파악 불가, 대상 없음, 되묻기, 실행 결과, 확인 요청 등)
    message: str | None = None
    # 삭제·수정 결과, 또는 일정 초안(create)의 확인 대기 정보
    command: CommandResult | None = None
    # 확인 카드에서 말로 초안을 고쳤을 때 바뀐 항목 (강조 표시용)
    draft_changes: list[DraftField] = []
