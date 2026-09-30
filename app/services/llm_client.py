from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from datetime import date as dt_date
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError
from app.i18n import Language, non_compliance_category_label, to_language
from app.models.enums import Importance, NonComplianceCategory
from app.models.event import Event
from app.models.important_date_range import ImportantDateRange
from app.models.location import Location
from app.models.user import User
from app.schemas.persona import PersonaRead

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

PromptTask = Literal["event_slot_fill", "draft_edit", "compliance_feedback", "daily_checkin"]
LANGUAGE_NAMES: dict[Language, str] = {"ko": "한국어(Korean)", "en": "영어(English)"}

# 지시문·요약이 한국어여도 모델이 따라 쓰지 않도록, 대상 언어로 쓴 지시를 한 번 더 붙인다.
_NATIVE_LANGUAGE_RULES: dict[Language, str] = {
    "ko": "반드시 한국어로만 답하세요.",
    "en": "Respond only in English.",
}
_NATIVE_QUESTION_LANGUAGE_RULES: dict[Language, str] = {
    "ko": "되묻는 질문은 반드시 한국어로만 작성하세요.",
    "en": "Write every clarifying question in English only.",
}

DEFAULT_PERSONA_BLOCK = "[페르소나]\n일정 관리 앱의 다정한 코치. 나무라지 않고 공감하며 격려한다."

SlotName = Literal[
    "title", "date", "frequency", "by_day", "interval", "recurrence_start", "start_time", "end_time", "importance",
    "date_range_id",
]

SLOT_NAMES: tuple[SlotName, ...] = (
    "title",
    "date",
    "frequency",
    "by_day",
    "interval",
    "recurrence_start",
    "start_time",
    "end_time",
    "importance",
    "date_range_id",
)

# FR-2 v3.6 의도 분류와 삭제·수정 대상 "설명". LLM은 이벤트 ID를 고르지 않는다 — 어떤 이벤트인지는
# 백엔드(event_command_service)가 이 설명으로 DB에서 결정적으로 찾는다.
Intent = Literal["create", "delete", "update", "list", "unknown"]
TargetKind = Literal["event", "date_range"]
_COMMAND_FIELDS: dict[str, dict[str, object]] = {
    "intent": {"type": "string", "enum": ["create", "delete", "update", "list", "unknown"]},
    "answers_previous_question": {
        "type": "boolean",
        "description": "앱이 방금 되물은 질문에 대한 답이면 true, 새 요청이면 false",
    },
    "target_kind": {"type": "string", "enum": ["event", "date_range"], "description": "일정이면 event, 반복 기간 자체면 date_range"},
    "range_name": {"type": ["string", "null"], "description": "만들 기간의 이름, 또는 바꾸거나 지울 기간의 이름"},
    "range_start": {"type": ["string", "null"], "description": "기간 시작일 YYYY-MM-DD(연도를 말하지 않았으면 MM-DD)"},
    "range_end": {"type": ["string", "null"], "description": "기간 종료일 YYYY-MM-DD(연도를 말하지 않았으면 MM-DD)"},
    "range_new_name": {"type": ["string", "null"], "description": "기간 이름을 바꿀 때 새 이름"},
    "target_title": {"type": ["string", "null"], "description": "삭제/수정할 일정 제목"},
    "target_date": {"type": ["string", "null"], "description": "특정 날짜 YYYY-MM-DD"},
    "target_all": {"type": "boolean", "description": "'전부/모두'면 true"},
    "target_scope": {"type": ["string", "null"], "enum": ["instance", "series", None]},
    "new_start_time": {"type": ["string", "null"], "description": "24시간제 HH:MM"},
    "new_end_time": {"type": ["string", "null"], "description": "24시간제 HH:MM"},
    "new_title": {"type": ["string", "null"]},
    "new_importance": {"type": ["integer", "null"], "enum": [1, 2, 3, 4, 5, 6, None]},
    "location_action": {"type": ["string", "null"], "enum": ["set", "remove", None], "description": "장소를 넣거나 바꾸면 set, 빼면 remove"},
    "new_location_name": {"type": ["string", "null"], "description": "넣거나 바꿀 장소 이름"},
    "target_weekday": {
        "type": ["string", "null"],
        "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU", None],
        "description": "'월요일의 ○○'처럼 요일로 대상을 가리킬 때",
    },
}

# FR-2 슬롯필링 결과의 JSON 스키마. frequency/by_day는 RRULE FREQ/BYDAY 값과 그대로
# 이어지게 해서, event_parse_service가 "FREQ=WEEKLY;BYDAY=MO" 같은 recurrence_rule을
# 바로 조립할 수 있게 한다 (app/services/recurrence.py의 build_recurrence_rule 참고).
_EVENT_SLOT_JSON_SCHEMA = {
    "name": "event_slot_fill",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": ["string", "null"]},
            "event_type": {
                "type": ["string", "null"],
                "enum": ["scheduled", "deadline", None],
                "description": "마감(과제 제출 등)이면 deadline — start_time은 null, end_time이 마감 시각",
            },
            "date": {
                "type": ["string", "null"],
                "description": "일정 날짜 YYYY-MM-DD. 사용자가 연도를 말하지 않았으면 MM-DD",
            },
            "frequency": {
                "type": ["string", "null"],
                "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY", None],
            },
            "by_day": {
                "type": ["array", "null"],
                "items": {
                    "type": "string",
                    "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                },
                "description": "frequency가 WEEKLY일 때 반복 요일들 (예: ['MO'])",
            },
            "interval": {"type": ["integer", "null"], "description": "반복 간격. 격주·2주마다는 2 (기본 1)"},
            "recurrence_start": {
                "type": ["string", "null"],
                "description": "반복 일정의 첫 회차 날짜 YYYY-MM-DD(연도를 말하지 않았으면 MM-DD)",
            },
            "start_time": {
                "type": ["string", "null"],
                "description": "24시간제 HH:MM",
            },
            "end_time": {
                "type": ["string", "null"],
                "description": "24시간제 HH:MM",
            },
            "importance": {
                "type": ["integer", "null"],
                "enum": [1, 2, 3, 4, 5, 6, None],
                "description": "null=없음(수면), 1~5, 6=MAX",
            },
            "date_range_id": {"type": ["integer", "null"]},
            "new_date_range": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "name": {"type": ["string", "null"]},
                            "start_date": {"type": ["string", "null"]},
                            "end_date": {"type": "string"},
                        },
                        "required": ["name", "start_date", "end_date"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                ],
                "description": "등록된 기간 대신 새로 만들 반복 기간",
            },
            "missing_slots": {
                "type": "array",
                "items": {"type": "string", "enum": list(SLOT_NAMES)},
            },
            "clarifying_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "slot": {"type": "string", "enum": list(SLOT_NAMES)},
                        "question": {"type": "string"},
                    },
                    "required": ["slot", "question"],
                    "additionalProperties": False,
                },
            },
            **_COMMAND_FIELDS,
        },
        "required": list(SLOT_NAMES)
        + ["event_type", "new_date_range", "missing_slots", "clarifying_questions"]
        + list(_COMMAND_FIELDS),
        "additionalProperties": False,
    },
}


class DateRangeOption(BaseModel):
    """LLM에게 후보로 제시할, 사용자가 이미 등록해둔 ImportantDateRange 하나."""

    id: int
    name: str
    start_date: date
    end_date: date


class NewDateRangeSlot(BaseModel):
    """대화 중에 새로 만들 반복 기간. 날짜는 YYYY-MM-DD 또는 연도 없는 MM-DD, 이름·시작일은 없을 수 있다."""

    name: str | None = None
    start_date: str | None = None
    end_date: str


# 되묻는 질문이 가리키는 항목. 슬롯필링 슬롯 외에, 초안을 고치다가 새 장소의 이동 시간을 물을 때 location을 쓴다.
QuestionSlot = Literal[
    "title", "date", "frequency", "by_day", "interval", "recurrence_start", "start_time", "end_time", "importance",
    "date_range_id", "location",
]


class ClarifyingQuestion(BaseModel):
    slot: QuestionSlot
    question: str


class EventSlotFillResult(BaseModel):
    """FR-2 슬롯필링 결과. 값이 None이어도 missing_slots에 없으면 "명시적으로 없음"
    (예: importance=None은 수면)이고, missing_slots에 있으면 "아직 모름 -> 되물어야 함"이다.
    """

    title: str | None = None
    event_type: Literal["scheduled", "deadline"] | None = None
    date: str | None = None  # YYYY-MM-DD, 연도를 말하지 않았으면 MM-DD (event_parse_service가 날짜로 정한다)
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"] | None = None
    by_day: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] | None = None
    interval: int | None = None
    recurrence_start: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    importance: Importance | None = None
    date_range_id: int | None = None
    new_date_range: NewDateRangeSlot | None = None
    missing_slots: list[SlotName] = Field(default_factory=list)
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    # v3.6: 새 필드가 없는 응답(기존 테스트의 가짜 응답 등)은 일정 추가(create)로 본다.
    intent: Intent = "create"
    # 이 발화가 앱이 방금 되물은 질문에 대한 답인지. 아니면 백엔드가 이전 대화 상태를 버리고 새 요청으로 처리한다.
    answers_previous_question: bool = True
    target_kind: TargetKind = "event"
    range_name: str | None = None
    range_start: str | None = None
    range_end: str | None = None
    range_new_name: str | None = None
    target_title: str | None = None
    target_date: dt_date | None = None  # 이 모델에 date 슬롯 필드가 있어 타입은 별칭으로 쓴다
    target_all: bool = False
    target_scope: Literal["instance", "series"] | None = None
    new_start_time: str | None = None
    new_end_time: str | None = None
    new_title: str | None = None
    new_importance: Importance | None = None
    location_action: Literal["set", "remove"] | None = None
    new_location_name: str | None = None
    target_weekday: Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"] | None = None

    @property
    def is_complete(self) -> bool:
        return not self.missing_slots


class LLMClientError(AppError):
    """LLM 클라이언트 관련 에러의 공통 베이스. 상태 코드는 하위 클래스가 정하고,
    app.core.exceptions의 전역 핸들러가 HTTP 응답으로 바꾼다."""

    status_code = 502
    log_level = logging.WARNING


class LLMConfigError(LLMClientError):
    """LLM_API_KEY 등 필수 설정이 빠졌을 때. 서버 설정 문제이지 요청 자체의
    잘못이 아니다."""

    status_code = 500
    log_level = logging.ERROR


class LLMRequestError(LLMClientError):
    """LLM API 호출 자체가 실패했을 때 (네트워크 오류, 4xx/5xx 응답 등). 진짜
    upstream 문제이므로 502 Bad Gateway가 적절하다."""


class LLMResponseParsingError(LLMClientError):
    """LLM이 응답은 했지만 JSON 파싱에 실패했거나 우리가 기대한 스키마와 다를 때.
    upstream이 완전히 죽은 게 아니라 우리가 처리 못 할 데이터를 줬다는 뜻이므로
    502가 아니라 422로 다뤄야 한다."""

    status_code = 422


_EVENT_SLOT_INSTRUCTIONS = (
    "너는 일정 관리 앱의 자연어 이벤트 파서다. 이 앱은 한 번만 있는 단발 일정과 반복 일정을 모두 "
    "지원한다. 사용자의 발화에서 다음 슬롯을 추출해 JSON으로만 답한다: title, date, frequency, by_day, "
    "start_time, end_time, importance, date_range_id.\n"
    "- 반복 여부: 사용자가 '매일', '매주', '월수금마다', '격주'처럼 반복을 직접 말했을 때만 반복 일정이다. "
    "반복을 말하지 않았거나 '반복 없이', '한 번만', '이번만'처럼 답하면 단발 일정이다. 단발 일정이면 "
    "frequency, by_day, date_range_id를 모두 null로 두고 missing_slots에 넣지 않으며, 반복 여부를 묻지 않는다.\n"
    "- date는 단발 일정의 날짜다(반복 일정의 첫 날짜는 date가 아니라 recurrence_start에 적는다). "
    "'오늘', '내일', '다음 주 금요일'처럼 상대적인 표현은 오늘 날짜 기준으로 계산해 YYYY-MM-DD로 적는다. "
    "'10월 1일'처럼 연도 없이 말하면 연도를 붙이지 말고 MM-DD(예: 10-01)로 적는다. 단발 일정인데 날짜를 "
    "모르면 date를 missing_slots에 넣고 묻는다.\n"
    "- frequency는 반복 일정일 때만 DAILY/WEEKLY/MONTHLY/YEARLY 중 하나로 적는다. '매일'이면 DAILY, "
    "'매주'면 WEEKLY다.\n"
    "- interval은 반복 간격이다(기본 1). '2주마다', '격주', '한 주 걸러'는 WEEKLY에 interval 2, '3주마다'는 3, "
    "'이틀마다'는 DAILY에 2, '두 달마다'는 MONTHLY에 2다. '매주 화요일'과 '2주마다'를 함께 말하면 모순이 아니라 "
    "'격주 화요일'(WEEKLY, by_day [\"TU\"], interval 2)이므로 되묻지 않는다. 서로 다른 요일이나 다른 일정을 가리켜 정말 "
    "애매할 때만 묻는다.\n"
    "- recurrence_start는 반복 일정의 첫 회차 날짜다('9/22부터' → 09-22). 날짜 형식은 date와 같다. 말하지 않으면 null로 "
    "두고 묻지 않는다(앱이 반복 기간 시작 뒤 첫 해당 요일로 정한다).\n"
    "- by_day는 frequency가 WEEKLY일 때 반복 요일들을 MO/TU/WE/TH/FR/SA/SU "
    "코드의 배열로 담는다 (예: '매주 월요일'이면 [\"MO\"], '매주 화, 목'이면 "
    "[\"TU\", \"TH\"]). DAILY/MONTHLY/YEARLY면 보통 필요 없으니 빈 배열로 둔다.\n"
    "- event_type: '과제 제출날이야', '~까지 내야 해', '마감', '제출'처럼 마감을 말하면 deadline이다. deadline이면 "
    "start_time은 null이고 end_time에 마감 시각을 적는다('11:30 pm'은 23:30). 그 밖에는 scheduled.\n"
    "- start_time, end_time은 24시간제 HH:MM 형식이다. '1시간 동안'처럼 길이만 말하면 end_time은 "
    "start_time에 그 길이를 더한 시각이다. '8시'처럼 오전/오후가 분명하지 않으면 오전으로 적고 직접 묻지 "
    "않는다(필요하면 앱이 한 번 되묻는다). '오후 8시', '저녁 8시', '20시'처럼 분명하면 그대로 변환한다.\n"
    "- importance는 null(없음/수면), 1(개인 여가), 2(타인 연관 약속), "
    "3(의무이지만 출석 체크 없음), 4(공식적 의무/평가), 5(반드시 지켜야 함), "
    "6(MAX, 5보다 예외적으로 중요) 중 하나다. 발화에서 유추할 수 없으면 슬롯을 "
    "채우지 말고 missing_slots에 넣는다.\n"
    "- 반복 기간(언제까지 반복하는지)은 반복 일정일 때만 묻고 쓴다. 단발 일정이면 date_range_id와 "
    "new_date_range 모두 null이고 묻지 않는다. 등록된 기간 후보(이름·시작일·종료일)를 그대로 쓰면 date_range_id에 "
    "그 id를 적는다. 사용자가 다른 날짜까지라고 하거나 새 기간을 말하면 date_range_id는 null로 두고 new_date_range에 "
    "{name, start_date, end_date}를 적는다. '2학기 시작부터', '학기 끝까지'처럼 등록된 기간의 시작일·종료일을 "
    "가리키는 말은 후보 목록의 실제 날짜로 바꿔 적는다. 시작일을 말하지 않으면 start_date는 null, 이름을 "
    "말하지 않으면 name은 null이다(앱이 정하고 알려 준다). 이름은 사용자가 말한 그대로 적는다(예: 'Lecture End "
    "Date'). 기간을 새로 만들 수 없다고 답하지 않는다. 기간을 전혀 모를 때만 date_range_id를 missing_slots에 넣는다.\n"
    "- 확실하게 알아낸 슬롯은 missing_slots에 넣지 않는다. 값을 못 정한 슬롯만 "
    "missing_slots에 넣고, 그 각각에 대해 사용자에게 되물을 자연스러운 질문을 "
    "clarifying_questions에 함께 준다 (질문 언어는 [질문 언어] 블록을 따른다).\n"
    "- '최근 대화'가 함께 주어지면 같은 대화에서 앱이 물은 것과 사용자가 답한 것이다. 사용자가 이미 답했거나 "
    "분명히 말한 내용은 다시 묻지 말고 그 답을 반영한다.\n"
    "- '이미 확정된 슬롯'이 함께 주어질 수 있다. 이는 이전 대화 턴에서 이미 "
    "알아낸 값이다. 최신 발화가 그 값을 바꾸라고 명시하지 않는 한 그대로 결과에 "
    "포함하고 missing_slots에 넣지 않는다. 최신 발화가 다른 값으로 정정하면 그 "
    "값으로 덮어쓴다.\n"
    "[이어지는 답인지] answers_previous_question: '최근 대화'의 마지막 앱 말이 되묻는 질문이고 이 발화가 그 답이면 "
    "true다. 새 날짜와 새 제목, '추가해줘', '~날이야'처럼 새 요청이 분명하면 false다. 앞선 요청이 없으면 false.\n"
    "[의도 분류] 먼저 intent를 정한다: 새 일정을 만들려는 발화는 create, 기존 일정을 없애려는 발화"
    "(삭제·취소·없애줘)는 delete, 기존 일정의 시간·제목·중요도를 바꾸려는 발화는 update, 목록을 보여 달라는 "
    "발화는 list, 일정 관리와 무관하면 unknown이다. 대상이 일정이면 target_kind=event, 반복 기간(학기, "
    "'Lecture End Date'처럼 이름 붙은 기간) 자체를 만들거나 바꾸거나 지우거나 보여 달라는 것이면 "
    "target_kind=date_range다. 일정을 만들면서 반복 기간을 말하는 것은 일정 생성(event)이다.\n"
    "- target_kind=date_range면 슬롯·target_*·new_* 필드는 비워 두고 range_* 만 쓴다. create: range_name(말한 "
    "이름, 없으면 null), range_start, range_end. update: range_name은 바꿀 기간의 이름(등록된 기간 목록에서 가장 "
    "가까운 이름), 바뀌는 값만 range_start/range_end/range_new_name에 적는다. delete: range_name. list: 모두 null. "
    "날짜는 YYYY-MM-DD, 연도를 말하지 않았으면 MM-DD다. target_kind=event면 range_* 는 모두 null이다.\n"
    "- create일 때만 위의 슬롯 규칙을 따른다. create가 아니면 슬롯 필드는 모두 null, missing_slots와 "
    "clarifying_questions는 빈 배열로 둔다.\n"
    "- delete/update면 대상을 '설명'만 한다: target_title에는 '등록된 일정 제목' 목록 중 사용자가 말한 "
    "일정과 가장 가까운 제목을 그대로 적는다(목록에 없으면 사용자가 말한 표현). 특정 날짜를 말하면 "
    "target_date(YYYY-MM-DD, '오늘'·'내일'·'이번 주 금요일'은 오늘 날짜 기준으로 계산)를 적는다. "
    "'전부/모두/다'면 target_all=true. '이번만/그날만'이면 target_scope=instance, '반복 전체/매번/앞으로 "
    "전부'면 series, 알 수 없으면 null.\n"
    "- 장소: '장소 넣어줘/바꿔줘 ○○'면 location_action=set, new_location_name에 말한 이름(등록된 장소가 비슷하면 그 "
    "이름). 이름 없이 '장소 추가해줘'만 말하면 location_action=set, new_location_name=null. '장소 빼줘'면 remove. "
    "'월요일의 ○○'처럼 요일로 가리키면 target_weekday에 그 요일(MO~SU)을 적고 target_date는 null로 둔다.\n"
    "- update면 바꿀 값만 new_start_time/new_end_time(HH:MM)/new_title/new_importance에 채우고 "
    "나머지는 null로 둔다. 시작 시각만 말하면 new_end_time은 null로 둔다(지속 시간은 백엔드가 유지한다).\n"
    "- create와 unknown이면 target_* 는 null(target_all은 false), new_* 는 null이다.\n"
    "- '진행 중인 요청'이 함께 주어지면 이전 턴의 삭제·수정 요청이다. 사용자의 답을 반영해 같은 intent로 "
    "target_*/new_* 를 다시 채운다(번호로 고르면 그 후보의 제목과 날짜를 적는다)."
)


DraftDecision = Literal["edit", "confirm", "cancel", "other_event_command", "unclear"]

# 확인 대기 중인 초안을 고치는 말. 바뀐 항목만 채우고 나머지는 null(바꾸지 않음)이다.
_DRAFT_EDIT_JSON_SCHEMA = {
    "name": "draft_edit",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["edit", "confirm", "cancel", "other_event_command", "unclear"]},
            "title": {"type": ["string", "null"]},
            "event_type": {"type": ["string", "null"], "enum": ["scheduled", "deadline", None]},
            "date": {"type": ["string", "null"], "description": "YYYY-MM-DD, 연도를 말하지 않았으면 MM-DD"},
            "start_time": {"type": ["string", "null"], "description": "24시간제 HH:MM"},
            "end_time": {"type": ["string", "null"], "description": "24시간제 HH:MM (마감이면 마감 시각)"},
            "duration_minutes": {"type": ["integer", "null"]},
            "importance": {"type": ["integer", "null"], "enum": [1, 2, 3, 4, 5, 6, None]},
            "importance_none": {"type": "boolean", "description": "중요도를 '없음'으로 바꿀 때 true"},
            "recurrence": {"type": ["string", "null"], "enum": ["set", "remove", None]},
            "frequency": {"type": ["string", "null"], "enum": ["DAILY", "WEEKLY", "MONTHLY", "YEARLY", None]},
            "by_day": {
                "type": ["array", "null"],
                "items": {"type": "string", "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]},
            },
            "interval": {"type": ["integer", "null"], "description": "반복 간격 (매주로=1, 격주로=2)"},
            "recurrence_start": {"type": ["string", "null"], "description": "반복 첫 회차 날짜"},
            "date_range_id": {"type": ["integer", "null"]},
            "new_date_range": _EVENT_SLOT_JSON_SCHEMA["schema"]["properties"]["new_date_range"],
            "location_name": {"type": ["string", "null"]},
            "travel_minutes": {"type": ["integer", "null"]},
            "unsupported": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "decision", "title", "event_type", "date", "start_time", "end_time", "duration_minutes", "importance",
            "importance_none", "recurrence", "frequency", "by_day", "interval", "recurrence_start", "date_range_id",
            "new_date_range",
            "location_name", "travel_minutes", "unsupported",
        ],
        "additionalProperties": False,
    },
}


class DraftEditResult(BaseModel):
    """확인 대기 중인 초안에 대한 한 마디. 값이 null이면 그 항목은 바꾸지 않는다."""

    decision: DraftDecision = "edit"
    title: str | None = None
    event_type: Literal["scheduled", "deadline"] | None = None
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    duration_minutes: int | None = None
    importance: Importance | None = None
    importance_none: bool = False
    recurrence: Literal["set", "remove"] | None = None
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY", "YEARLY"] | None = None
    by_day: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] | None = None
    interval: int | None = None
    recurrence_start: str | None = None
    date_range_id: int | None = None
    new_date_range: NewDateRangeSlot | None = None
    location_name: str | None = None
    travel_minutes: int | None = None
    unsupported: list[str] = Field(default_factory=list)


_DRAFT_EDIT_INSTRUCTIONS = (
    "너는 일정 관리 앱에서 사용자가 '이 내용으로 만들까요?' 확인 카드를 보고 있을 때 한 말을 해석한다. 사용자의 말은 "
    "기본적으로 지금 보고 있는 초안을 고치는 말이다. JSON으로만 답한다.\n"
    "- decision: 초안을 고치는 말이면 edit. '좋아', '응 만들어줘', '그대로 해'처럼 이대로 만들라는 말이면 confirm. "
    "'취소', '안 만들래'면 cancel. '기존 물리 퀴즈를 지워줘'처럼 '등록된 일정 제목' 중 다른 일정을 분명히 가리켜 지우거나 "
    "바꾸라는 말일 때만 other_event_command다. '바꿔줘', '빼줘'만으로는 다른 일정이 아니라 초안을 고치는 말이다. 무엇을 "
    "바꿀지 알 수 없으면 unclear.\n"
    "- 바뀐 항목만 채우고 나머지는 null(importance_none은 false, unsupported는 빈 배열)로 둔다. 초안 전체를 다시 쓰지 않는다.\n"
    "- 시간: start_time/end_time은 24시간제 HH:MM. 오전/오후를 말하지 않으면 현재 초안 시각과 같은 오전/오후로 본다 "
    "(현재 20:00인 초안에 '7시로'는 19:00). 시작만 바꾸면 end_time은 null로 둔다(길이는 앱이 유지한다). '2시간으로'처럼 "
    "길이를 말하면 duration_minutes에 적는다. date는 YYYY-MM-DD, 연도를 말하지 않았으면 MM-DD.\n"
    "- event_type: '마감으로'면 deadline(마감 시각은 end_time), '일반 일정으로'면 scheduled.\n"
    "- importance: 1~5, 'MAX'는 6. '중요도 없음'이면 importance_none=true.\n"
    "- 반복: '매주 수요일로 반복해줘'면 recurrence=set, frequency, by_day. '반복 빼줘'면 recurrence=remove. '매주로 바꿔줘'는 "
    "interval 1, '격주로'는 interval 2(recurrence는 null). '10/6부터'처럼 첫 회차를 바꾸면 recurrence_start. 반복 기간을 "
    "함께 말하면 등록된 기간 후보의 id를 date_range_id에, 새 기간이면 new_date_range에 {name, start_date, end_date}를 적는다.\n"
    "- 장소: '장소는 Bahen이야'면 location_name에 말한 이름을 그대로(등록된 장소 목록에 비슷한 이름이 있으면 그 이름을) 적는다. "
    "이동 시간을 말하면 travel_minutes(분)에 적는다.\n"
    "- 이 앱이 초안에서 바꿀 수 있는 것은 제목, 날짜, 시간, 종류(일반/마감), 중요도, 반복과 반복 기간, 장소뿐이다. 메모, 알림 "
    "시각, 참석자, 색상처럼 그 밖의 것을 요청하면 그 항목 이름을 사용자 언어로 unsupported에 넣는다.\n"
    "- '최근 대화'가 함께 주어지면 같은 대화에서 앱이 물은 것과 사용자가 답한 것이다. 이미 말한 내용은 다시 묻지 않고 반영한다."
)


def _build_locations_block(location_names: list[str]) -> str:
    return f"등록된 장소: {json.dumps(location_names, ensure_ascii=False)}"


def fill_draft_edit_for_user(
    db: Session,
    user_id: int,
    utterance: str,
    *,
    draft: dict[str, object],
    history: list["ConversationTurn"] | None = None,
    reference_date: date | None = None,
    http_client: httpx.Client | None = None,
) -> DraftEditResult:
    """확인 카드가 떠 있을 때 들어온 말을 '초안에서 바뀐 항목'으로 받는다. 합치기·검증은 호출부(백엔드)가 한다."""
    user = db.get(User, user_id)
    lines = [
        f"오늘 날짜: {(reference_date or date.today()).isoformat()}",
        f"현재 초안: {json.dumps(draft, ensure_ascii=False, default=str)}",
    ]
    if history:
        lines.append("최근 대화:")
        lines.extend(f"{_SPEAKERS[role]}: {text}" for role, text in history)
    lines.append(f"사용자 발화: {utterance}")
    payload = _build_payload(
        "draft_edit",
        [
            _DRAFT_EDIT_INSTRUCTIONS,
            _build_question_language_block(to_language(user.preferred_language if user else "ko")),
            _build_date_ranges_block(get_date_range_options(db, user_id)),
            _build_event_titles_block(get_event_titles(db, user_id)),
            _build_locations_block(get_location_names(db, user_id)),
        ],
        "\n".join(lines),
        response_format={"type": "json_schema", "json_schema": _DRAFT_EDIT_JSON_SCHEMA},
    )
    raw_content = _call_chat_completion(payload, http_client)
    try:
        return DraftEditResult.model_validate(json.loads(raw_content))
    except Exception as exc:  # JSON 오류, pydantic ValidationError 등
        raise LLMResponseParsingError(f"LLM 초안 수정 응답이 예상한 형식이 아닙니다: {raw_content!r}") from exc


def _prompt_cache_key(task: PromptTask | ResponsesTask, cacheable_prefix: str) -> str:
    digest = hashlib.sha256(cacheable_prefix.encode("utf-8")).hexdigest()[:16]
    return f"{task}:{digest}"


def _build_payload(
    task: PromptTask,
    cacheable_blocks: list[str],
    dynamic_content: str,
    **extra: object,
) -> dict[str, object]:
    """모든 LLM 요청 payload를 만드는 단일 진입점.

    프롬프트 캐시는 프리픽스가 바이트 단위로 같아야 적중하므로, 자주 안 바뀌는
    블록(작업 지시 → 페르소나 → 사용자별 목록 순, 공유 범위가 넓은 것부터)은 system
    메시지에 모으고 매 요청마다 바뀌는 값은 마지막 user 메시지에만 둔다.
    prompt_cache_key는 같은 프리픽스끼리 같은 캐시 서버로 라우팅되게 한다.
    """
    cacheable_prefix = "\n\n".join(cacheable_blocks)
    return {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": cacheable_prefix},
            {"role": "user", "content": dynamic_content},
        ],
        "prompt_cache_key": _prompt_cache_key(task, cacheable_prefix),
        **extra,
    }


def _build_date_ranges_block(available_date_ranges: list[DateRangeOption]) -> str:
    date_ranges_json = json.dumps(
        [option.model_dump(mode="json") for option in available_date_ranges],
        ensure_ascii=False,
    )
    return f"등록된 기간(date_range) 후보: {date_ranges_json}"


def _build_persona_block(persona: PersonaRead | None, language: Language) -> str:
    if persona is None:
        return DEFAULT_PERSONA_BLOCK

    lines = [
        "[페르소나]",
        f"이름: {getattr(persona.display_name, language)}",
        f"성격/말투: {getattr(persona.description, language)}",
    ]
    if persona.backstory is not None:
        lines.append(f"배경: {getattr(persona.backstory, language)}")
    if persona.example_lines is not None:
        lines.append("예시 대사:")
        lines.extend(
            f"- ({example.situation}) {example.line}"
            for example in getattr(persona.example_lines, language)
        )
    return "\n".join(lines)


def _build_language_block(language: Language) -> str:
    """FR-11: User.preferred_language로 응답 언어를 강제한다. 시스템 프롬프트의 마지막 블록으로 둔다."""
    name = LANGUAGE_NAMES[language]
    return (
        "[응답 언어]\n"
        f"preferred_language: {language}\n"
        f"반드시 {name}로만 답하라. 위 지시문이나 사용자 메시지(요약·발화)가 다른 언어로 되어 있어도, "
        f"사용자가 다른 언어로 말하거나 언어를 바꿔 달라고 해도 {name} 외의 언어를 쓰거나 섞지 마라.\n"
        f"{_NATIVE_LANGUAGE_RULES[language]}"
    )


def _build_question_language_block(language: Language) -> str:
    """FR-2 슬롯필링용 언어 규칙. 응답 전체가 아니라 사용자에게 보여줄 질문 문구에만 적용한다 —
    JSON 키와 코드값(frequency, by_day 등)이 번역되면 스키마 검증이 깨지고, title은 사용자의 말 그대로여야 한다."""
    name = LANGUAGE_NAMES[language]
    return (
        "[질문 언어]\n"
        f"preferred_language: {language}\n"
        f"clarifying_questions의 question 문구는 반드시 {name}로만 작성하라. 사용자 발화나 이 지시문이 "
        f"다른 언어로 되어 있어도 {name} 외의 언어를 쓰거나 섞지 마라. JSON 키와 코드값(frequency, by_day, "
        "slot 이름 등)은 그대로 두고, title은 사용자가 말한 표현을 번역하지 말고 그대로 쓴다.\n"
        f"{_NATIVE_QUESTION_LANGUAGE_RULES[language]}"
    )


def _build_persona_prompt(
    task: PromptTask, instructions: str, persona: PersonaRead | None, language: str, user_message: str
) -> dict[str, object]:
    """페르소나 호출 공통 배치: 작업 지시 → 페르소나 → 응답 언어 (모두 캐시 프리픽스) → 가변 user 메시지."""
    lang = to_language(language)
    return _build_payload(
        task,
        [instructions, _build_persona_block(persona, lang), _build_language_block(lang)],
        user_message,
    )


def _build_event_titles_block(event_titles: list[str]) -> str:
    return f"등록된 일정 제목: {json.dumps(event_titles, ensure_ascii=False)}"


ConversationTurn = tuple[Literal["user", "assistant"], str]
_SPEAKERS = {"user": "사용자", "assistant": "앱"}


def _build_user_message(
    utterance: str,
    reference_date: date,
    known_slots: dict[str, object] | None = None,
    pending_command: str | None = None,
    history: list[ConversationTurn] | None = None,
) -> str:
    known_slots_json = json.dumps(known_slots or {}, ensure_ascii=False, default=str)
    lines = [f"오늘 날짜: {reference_date.isoformat()}", f"이미 확정된 슬롯: {known_slots_json}"]
    if pending_command:
        lines.append(f"진행 중인 요청: {pending_command}")
    if history:
        lines.append("최근 대화:")
        lines.extend(f"{_SPEAKERS[role]}: {text}" for role, text in history)
    lines.append(f"사용자 발화: {utterance}")
    return "\n".join(lines)


def _build_request_payload(
    utterance: str,
    available_date_ranges: list[DateRangeOption],
    reference_date: date,
    known_slots: dict[str, object] | None = None,
    language: str = "ko",
    event_titles: list[str] | None = None,
    pending_command: str | None = None,
    history: list[ConversationTurn] | None = None,
) -> dict[str, object]:
    # 공유 범위 순: 작업 지시(전체 공통) → 질문 언어(언어별) → 등록된 기간·일정 제목(사용자별, 자주 안 바뀜)
    return _build_payload(
        "event_slot_fill",
        [
            _EVENT_SLOT_INSTRUCTIONS,
            _build_question_language_block(to_language(language)),
            _build_date_ranges_block(available_date_ranges),
            _build_event_titles_block(event_titles or []),
        ],
        _build_user_message(utterance, reference_date, known_slots, pending_command, history),
        response_format={"type": "json_schema", "json_schema": _EVENT_SLOT_JSON_SCHEMA},
    )


def _parse_response(raw_content: str) -> EventSlotFillResult:
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise LLMResponseParsingError(f"LLM 응답이 유효한 JSON이 아닙니다: {raw_content!r}") from exc

    try:
        return EventSlotFillResult.model_validate(data)
    except Exception as exc:  # pydantic ValidationError 등
        raise LLMResponseParsingError(f"LLM 응답이 예상한 스키마와 다릅니다: {data!r}") from exc


class TokenUsage(BaseModel):
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int  # reasoning_tokens를 포함한 값 (과금 기준)
    reasoning_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return self.prompt_tokens - self.cached_tokens


class ChatCompletionResult(BaseModel):
    content: str
    usage: TokenUsage | None


def _parse_usage(body: dict[str, Any]) -> TokenUsage | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details") or {}
    return TokenUsage(
        prompt_tokens=usage.get("prompt_tokens", 0),
        cached_tokens=details.get("cached_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
    )


class ChatUsageRecord(BaseModel):
    """Chat Completions 호출 한 번의 사용량. collect_chat_usage() 안에서만 모인다 (비교 스크립트용)."""

    task: str
    requested_model: str
    response_model: str | None
    reasoning_effort: str | None  # 요청에 넣은 값. None이면 보내지 않았다 = API 기본값
    usage: TokenUsage | None
    latency_ms: int


_chat_usage_sink: ContextVar[list[ChatUsageRecord] | None] = ContextVar("chat_usage_sink", default=None)


@contextmanager
def collect_chat_usage() -> Iterator[list[ChatUsageRecord]]:
    """이 블록 안(같은 스레드·컨텍스트)에서 성공한 Chat Completions 호출의 사용량을 목록으로 모은다."""
    records: list[ChatUsageRecord] = []
    token = _chat_usage_sink.set(records)
    try:
        yield records
    finally:
        _chat_usage_sink.reset(token)


def _log_usage(payload: dict[str, object], usage: TokenUsage | None) -> None:
    cache_key = payload.get("prompt_cache_key", "-")
    if usage is None:
        logger.info("[LLM usage] cache_key=%s usage 정보 없음", cache_key)
        return
    hit_ratio = usage.cached_tokens / usage.prompt_tokens if usage.prompt_tokens else 0.0
    logger.info(
        "[LLM usage] cache_key=%s prompt=%d cached=%d (%.0f%%) uncached=%d completion=%d reasoning=%d",
        cache_key,
        usage.prompt_tokens,
        usage.cached_tokens,
        hit_ratio * 100,
        usage.uncached_prompt_tokens,
        usage.completion_tokens,
        usage.reasoning_tokens,
    )


def _call_chat_completion(payload: dict[str, object], http_client: httpx.Client | None) -> str:
    """OpenAI Chat Completions를 호출해 message.content 문자열을 그대로 반환한다.

    구조화 출력(JSON schema)을 요청했다면 그 JSON 문자열이, 아니면 자유 텍스트가
    온다 — 파싱은 호출부의 책임이다.
    """
    return post_chat_completion(payload, http_client).content


def post_chat_completion(
    payload: dict[str, object], http_client: httpx.Client | None = None
) -> ChatCompletionResult:
    """Chat Completions 호출 결과(content + 토큰 사용량)를 반환하고, 사용량을 로그로 남긴다."""
    if not settings.llm_api_key:
        raise LLMConfigError("LLM_API_KEY가 설정되지 않았습니다 (.env 확인)")

    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}

    owns_client = http_client is None
    client = http_client or httpx.Client()
    started = time.perf_counter()
    try:
        response = client.post(CHAT_COMPLETIONS_URL, json=payload, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        raise LLMRequestError(f"LLM API 호출에 실패했습니다: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        raise LLMRequestError(f"LLM API가 오류를 반환했습니다 ({response.status_code}): {response.text}")

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise LLMResponseParsingError(f"LLM 응답 형식이 예상과 다릅니다: {response.text!r}") from exc

    usage = _parse_usage(body)
    _log_usage(payload, usage)
    sink = _chat_usage_sink.get()
    if sink is not None:
        sink.append(
            ChatUsageRecord(
                task=str(payload.get("prompt_cache_key", "-")).split(":")[0],
                requested_model=str(payload.get("model")),
                response_model=body.get("model"),
                reasoning_effort=payload.get("reasoning_effort"),  # type: ignore[arg-type]
                usage=usage,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
        )
    return ChatCompletionResult(content=content, usage=usage)


def fill_event_slots(
    utterance: str,
    *,
    available_date_ranges: list[DateRangeOption] | None = None,
    reference_date: date | None = None,
    known_slots: dict[str, object] | None = None,
    language: str = "ko",
    event_titles: list[str] | None = None,
    pending_command: str | None = None,
    history: list[ConversationTurn] | None = None,
    http_client: httpx.Client | None = None,
) -> EventSlotFillResult:
    """자연어 발화에서 title/frequency/by_day/start_time/end_time/importance/
    date_range_id를 채운다 (FR-2). 확정 못한 슬롯은 결과의
    missing_slots/clarifying_questions로 온다.

    available_date_ranges: 사용자가 이미 등록해둔 ImportantDateRange 후보 목록.
        LLM은 이 목록에 있는 id만 date_range_id로 쓸 수 있다.
    known_slots: 멀티턴 대화에서 이전 턴까지 이미 확정된 슬롯 값(예:
        SlotFillSession.known_slots()). 이걸 안 넘기면 이 함수는 매번 최신 발화만
        보고 판단하므로, 이전 턴에 알아낸 정보를 잃어버릴 수 있다.
    language: 되묻는 질문(clarifying_questions)의 언어. User.preferred_language를 넘긴다.
    http_client: 테스트에서 httpx.MockTransport로 응답을 주입하기 위한 훅. 생략하면
        settings.llm_api_key로 인증한 기본 클라이언트를 새로 만든다.
    """
    payload = _build_request_payload(
        utterance,
        available_date_ranges or [],
        reference_date or date.today(),
        known_slots,
        language,
        event_titles,
        pending_command,
        history,
    )
    raw_content = _call_chat_completion(payload, http_client)
    try:
        return _parse_response(raw_content)
    except LLMResponseParsingError as error:
        # 형식 검증에 실패하면 무엇이 틀렸는지 알려 주고 한 번만 다시 묻는다. 원본과 에러는 로그로 남긴다.
        logger.warning("[LLM] 슬롯필링 응답 검증 실패, 한 번 다시 요청: error=%s raw=%s", error, raw_content)
        retry = dict(payload)
        retry["messages"] = [
            *payload["messages"],  # type: ignore[misc]
            {"role": "assistant", "content": raw_content},
            {"role": "user", "content": f"방금 응답이 형식 검증에 실패했다: {error}. 같은 발화에 대해 스키마에 맞는 JSON으로 다시 답하라."},
        ]
        raw_retry = _call_chat_completion(retry, http_client)
        try:
            return _parse_response(raw_retry)
        except LLMResponseParsingError as second:
            logger.warning("[LLM] 다시 요청한 응답도 검증 실패: error=%s raw=%s", second, raw_retry)
            raise


def generate_compliance_feedback(
    category: NonComplianceCategory,
    reason_text: str | None,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
    http_client: httpx.Client | None = None,
) -> str:
    """FR-6: 미준수 사유에 대한 공감형 피드백 한두 문장을 생성한다.

    category가 OTHER이거나 reason_text가 채워진 경우에만 호출부가 이 함수를
    부른다 (버튼 클릭만으로 끝난 경우는 LLM을 아예 호출하지 않는 것이 FR-6의
    "LLM 우회 UI 숏컷"이다).
    """
    payload = build_compliance_feedback_payload(category, reason_text, persona=persona, language=language)
    content = _call_chat_completion(payload, http_client)
    return content.strip()


def build_compliance_feedback_payload(
    category: NonComplianceCategory,
    reason_text: str | None,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
) -> dict[str, object]:
    instructions = (
        "너는 일정 관리 앱의 페르소나다. 사용자가 계획한 일정을 지키지 못한 이유를 "
        "말했다. 아래 페르소나의 성격과 말투를 살리되, 사용자가 다음에 다시 해볼 "
        "마음이 들도록 짧게(1~2문장) 답하라."
    )

    # 프롬프트 골격이 한국어라 라벨도 ko로 넣는다. 응답 언어는 [응답 언어] 블록이 정한다.
    label = non_compliance_category_label(category, "ko")
    user_message = f"미준수 사유 카테고리: {label}"
    if reason_text:
        user_message += f"\n사용자가 직접 적은 이유: {reason_text}"

    return _build_persona_prompt("compliance_feedback", instructions, persona, language, user_message)


def generate_daily_checkin_reply(
    summary: str,
    utterance: str,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
    history: list["ConversationTurn"] | None = None,
    http_client: httpx.Client | None = None,
) -> str:
    """FR-8 저녁 9시 체크인 대화 한 턴을 생성한다.

    summary는 app.services.context_builder.build_daily_checkin_summary가 만든
    하루 요약이다 (완료한 일정은 개수만, 놓친 일정은 제목/시간/사유까지 상세히
    담겨 있다 - "컨텍스트 동적 로딩"). 요약은 매일 바뀌므로 캐시 프리픽스가 아닌
    user 메시지에 발화와 함께 넣는다.
    """
    payload = build_daily_checkin_payload(summary, utterance, persona=persona, language=language, history=history)
    content = _call_chat_completion(payload, http_client)
    return content.strip()


def build_daily_checkin_payload(
    summary: str,
    utterance: str,
    *,
    persona: PersonaRead | None = None,
    language: str = "ko",
    history: list["ConversationTurn"] | None = None,
) -> dict[str, object]:
    instructions = (
        "너는 일정 관리 앱의 페르소나다. 사용자와 저녁 체크인 대화를 나눈다. "
        "사용자 메시지에 오늘 하루 요약이 함께 온다 (완료한 일정은 개수만 적혀 있고, "
        "놓친 일정만 제목·시간·사유가 상세히 적혀 있다). 놓친 일정이 있다면 그것 "
        "위주로 묻고 격려하라. 놓친 일정이 없다면 짧게 칭찬하라. 아래 페르소나의 "
        "성격과 말투로 1~3문장 답하라. "
        "오늘 요약(계획 개수, 완료·놓친 일정)은 대화의 첫 답변에서만 짚는다. '이전 대화'가 있으면 이미 요약을 "
        "말한 것이니 되풀이하지 말고 사용자가 방금 한 말에 반응하라. "
        "사용자가 체크인과 무관한 작업(요리 레시피, 코드 작성, 번역, 숙제 풀이 등)을 부탁하면 캐릭터를 유지한 채 "
        "그 작업은 하지 않고, 오늘 하루 이야기로 자연스럽게 돌아오라."
    )
    lines = [f"오늘 요약:\n{summary}"]
    if history:
        lines.append("이전 대화:\n" + "\n".join(f"{'사용자' if role == 'user' else '페르소나'}: {text}" for role, text in history))
    lines.append(f"사용자 발화: {utterance}")
    user_message = "\n\n".join(lines)

    return _build_persona_prompt("daily_checkin", instructions, persona, language, user_message)


def get_date_range_options(db: Session, user_id: int) -> list[DateRangeOption]:
    """사용자가 등록해둔 ImportantDateRange 목록을 슬롯필링 후보로 변환한다.

    "언제까지 반복할까요?" 같은 date_range_id 질문에서 LLM이 실제 존재하는 기간
    중에서만 고르도록(지어내지 못하도록) fill_event_slots_for_user가 이 목록을
    컨텍스트로 넘긴다.
    """
    stmt = (
        select(ImportantDateRange)
        .where(ImportantDateRange.user_id == user_id)
        .order_by(ImportantDateRange.start_date)
    )
    date_ranges = db.execute(stmt).scalars().all()
    return [
        DateRangeOption(
            id=date_range.id,
            name=date_range.name,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
        for date_range in date_ranges
    ]


def fill_event_slots_for_user(
    db: Session,
    user_id: int,
    utterance: str,
    *,
    reference_date: date | None = None,
    known_slots: dict[str, object] | None = None,
    pending_command: str | None = None,
    history: list[ConversationTurn] | None = None,
    http_client: httpx.Client | None = None,
) -> EventSlotFillResult:
    """fill_event_slots를 호출하되, 이 user_id가 등록해둔 ImportantDateRange 전체를
    date_range_id 후보로 자동으로 함께 제시하고, 되묻는 질문은 그 사용자의
    preferred_language로 받는다 (FR-2, FR-11). 삭제·수정 대상을 정확히 짚도록 사용자의
    기존 일정 제목(ID 없이)도 함께 넣는다 (v3.6).
    """
    date_range_options = get_date_range_options(db, user_id)
    user = db.get(User, user_id)
    return fill_event_slots(
        utterance,
        available_date_ranges=date_range_options,
        reference_date=reference_date,
        known_slots=known_slots,
        language=user.preferred_language if user else "ko",
        event_titles=get_event_titles(db, user_id),
        pending_command=pending_command,
        history=history,
        http_client=http_client,
    )


def get_location_names(db: Session, user_id: int) -> list[str]:
    return sorted(db.execute(select(Location.name).where(Location.user_id == user_id)).scalars())


def get_event_titles(db: Session, user_id: int) -> list[str]:
    """사용자의 최상위 일정 제목 목록 (하위 이동시간 일정 제외, 중복 제거·정렬 — 프롬프트 캐시가 흔들리지 않게)."""
    titles = db.execute(
        select(Event.title).where(Event.user_id == user_id, Event.parent_event_id.is_(None)).distinct()
    ).scalars()
    return sorted(titles)


# --- Responses API (일정 어시스턴트) -------------------------------------------------
# gpt-5.6-luna는 Chat Completions에서 도구와 추론을 함께 쓸 수 없어서 어시스턴트는 /v1/responses를 쓴다.
# 대화 상태는 우리 DB가 갖고(previous_response_id 미사용) store=false로 호출하므로, 도구 결과를 넣어 이어서
# 부를 때 직전 응답의 output(암호화된 reasoning 포함)을 입력에 그대로 다시 넣어야 추론이 이어진다.

RESPONSES_URL = "https://api.openai.com/v1/responses"
RESPONSES_TIMEOUT_SECONDS = 20.0

ResponsesTask = Literal["assistant", "assistant_probe"]
ResponsesInputItem = dict[str, Any]

# 모델별로 받는 reasoning.effort 값 (OpenAI 모델 문서 기준, 2026-09 확인). 날짜 접미사가 붙은 스냅샷 이름도
# 접두사로 맞춘다. 목록에 없는 모델은 검증하지 않고 그대로 보낸다 (API가 판단).
REASONING_EFFORTS_BY_MODEL: dict[str, tuple[str, ...]] = {
    "gpt-5.6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.4-nano": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.4-mini": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5-nano": ("minimal", "low", "medium", "high"),
    "gpt-5-mini": ("minimal", "low", "medium", "high"),
}
# "추론 최소"를 뜻하는 두 이름은 모델마다 하나만 받으므로 서로 바꿔 준다.
_LOWEST_EFFORT_ALIASES = {"none": "minimal", "minimal": "none"}


def resolve_reasoning_effort(model: str, effort: str) -> str:
    """모델이 받는 effort 값으로 검증·변환한다. 지원하지 않는 값이면 LLMConfigError."""
    effort = effort.strip().lower()
    allowed = next(
        (values for prefix, values in sorted(REASONING_EFFORTS_BY_MODEL.items(), key=lambda kv: -len(kv[0])) if model.startswith(prefix)),
        None,
    )
    if allowed is None or effort in allowed:
        return effort
    alias = _LOWEST_EFFORT_ALIASES.get(effort)
    if alias in allowed:
        return alias
    raise LLMConfigError(
        f"ASSISTANT_REASONING_EFFORT={effort!r}는 {model}에서 지원하지 않습니다 (가능한 값: {', '.join(allowed)})"
    )


def validate_assistant_settings() -> str:
    """앱 시작 시 호출: 어시스턴트 모델과 effort 조합이 맞는지 확인하고 실제로 보낼 effort를 돌려준다."""
    return resolve_reasoning_effort(settings.assistant_model, settings.assistant_reasoning_effort)


class FunctionTool(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True

    def to_api(self) -> dict[str, Any]:
        return {"type": "function", **self.model_dump()}


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: str  # 모델이 준 JSON 문자열 그대로

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            raise LLMResponseParsingError(f"도구 {self.name} 인자가 JSON이 아닙니다: {self.arguments!r}") from exc
        if not isinstance(value, dict):
            raise LLMResponseParsingError(f"도구 {self.name} 인자가 객체가 아닙니다: {self.arguments!r}")
        return value


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


class ResponsesResult(BaseModel):
    response_id: str
    status: str
    text: str
    tool_calls: list[ToolCall]
    # 다음 호출 입력에 다시 넣을 원본 output 항목들 (reasoning, function_call, message)
    output_items: list[ResponsesInputItem]
    usage: ResponsesUsage | None
    latency_ms: int
    model: str
    reasoning_effort: str | None


class ResponsesCaller(Protocol):
    """ResponsesClient와 테스트용 가짜 클라이언트가 함께 따르는 인터페이스."""

    def create(
        self,
        *,
        task: ResponsesTask,
        instruction_blocks: list[str],
        tools: list[FunctionTool],
        input_items: list[ResponsesInputItem],
    ) -> ResponsesResult: ...


def user_message(text: str) -> ResponsesInputItem:
    return {"role": "user", "content": text}


def with_tool_outputs(
    input_items: list[ResponsesInputItem], result: ResponsesResult, outputs: dict[str, object]
) -> list[ResponsesInputItem]:
    """도구 결과를 넣어 이어서 부를 입력: 지금까지의 입력 + 직전 응답의 output 전체 + call_id별 도구 결과."""
    missing = [call.call_id for call in result.tool_calls if call.call_id not in outputs]
    if missing:
        raise ValueError(f"결과가 없는 도구 호출이 있습니다: {missing}")
    results: list[ResponsesInputItem] = []
    for call in result.tool_calls:
        value = outputs[call.call_id]
        output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        results.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
    return [*input_items, *result.output_items, *results]


def build_responses_payload(
    task: ResponsesTask,
    instruction_blocks: list[str],
    tools: list[FunctionTool],
    input_items: list[ResponsesInputItem],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    """고정 지시(instructions)와 도구 정의가 프롬프트 앞(캐시 프리픽스)에 오고, 매번 바뀌는 문맥·대화는
    input에만 둔다. temperature는 넣지 않는다 (추론 모델이 거부한다)."""
    instructions = "\n\n".join(instruction_blocks)
    tool_defs = [tool.to_api() for tool in tools]
    cacheable_prefix = instructions + json.dumps(tool_defs, ensure_ascii=False, sort_keys=True)
    model = model or settings.assistant_model
    return {
        "model": model,
        "instructions": instructions,
        "tools": tool_defs,
        "input": input_items,
        "reasoning": {"effort": resolve_reasoning_effort(model, reasoning_effort or settings.assistant_reasoning_effort)},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": _prompt_cache_key(task, cacheable_prefix),
    }


def _parse_responses_usage(body: dict[str, Any]) -> ResponsesUsage | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    return ResponsesUsage(
        input_tokens=usage.get("input_tokens", 0),
        cached_tokens=(usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        reasoning_tokens=(usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
    )


def _parse_responses_body(body: dict[str, Any], latency_ms: int) -> ResponsesResult:
    output = body.get("output")
    if not isinstance(output, list):
        raise LLMResponseParsingError(f"Responses API 응답에 output 배열이 없습니다: {body!r}")
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    try:
        for item in output:
            if item["type"] == "function_call":
                tool_calls.append(ToolCall(call_id=item["call_id"], name=item["name"], arguments=item.get("arguments", "")))
            elif item["type"] == "message":
                texts.extend(part["text"] for part in item.get("content", []) if part.get("type") == "output_text")
    except (KeyError, TypeError) as exc:
        raise LLMResponseParsingError(f"Responses API output 항목 형식이 예상과 다릅니다: {output!r}") from exc
    return ResponsesResult(
        response_id=body.get("id", ""),
        status=body.get("status", ""),
        text="".join(texts),
        tool_calls=tool_calls,
        output_items=output,
        usage=_parse_responses_usage(body),
        latency_ms=latency_ms,
        model=body.get("model", ""),
        reasoning_effort=(body.get("reasoning") or {}).get("effort"),
    )


def _log_responses_usage(payload: dict[str, object], result: ResponsesResult) -> None:
    usage = result.usage
    cache_key = payload.get("prompt_cache_key", "-")
    if usage is None:
        logger.info("[LLM usage] api=responses cache_key=%s latency_ms=%d usage 정보 없음", cache_key, result.latency_ms)
        return
    hit_ratio = usage.cached_tokens / usage.input_tokens if usage.input_tokens else 0.0
    logger.info(
        "[LLM usage] api=responses model=%s effort=%s cache_key=%s input=%d cached=%d (%.0f%%) output=%d "
        "reasoning=%d latency_ms=%d tool_calls=%d status=%s",
        result.model or payload.get("model"),
        result.reasoning_effort,
        cache_key,
        usage.input_tokens,
        usage.cached_tokens,
        hit_ratio * 100,
        usage.output_tokens,
        usage.reasoning_tokens,
        result.latency_ms,
        len(result.tool_calls),
        result.status,
    )


def post_responses(
    payload: dict[str, object],
    http_client: httpx.Client | None = None,
    *,
    timeout: float = RESPONSES_TIMEOUT_SECONDS,
) -> ResponsesResult:
    """/v1/responses 한 번 호출. 도구 호출·텍스트·토큰 사용량·지연 시간을 돌려주고 사용량을 로그로 남긴다."""
    if not settings.llm_api_key:
        raise LLMConfigError("LLM_API_KEY가 설정되지 않았습니다 (.env 확인)")

    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    owns_client = http_client is None
    client = http_client or httpx.Client()
    started = time.perf_counter()
    try:
        response = client.post(RESPONSES_URL, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LLMRequestError(f"LLM API 호출이 {timeout:g}초 안에 끝나지 않았습니다: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMRequestError(f"LLM API 호출에 실패했습니다: {exc}") from exc
    finally:
        if owns_client:
            client.close()
    latency_ms = round((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise LLMRequestError(f"LLM API가 오류를 반환했습니다 ({response.status_code}): {response.text}")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise LLMResponseParsingError(f"LLM 응답이 JSON이 아닙니다: {response.text!r}") from exc
    if not isinstance(body, dict):
        raise LLMResponseParsingError(f"LLM 응답 형식이 예상과 다릅니다: {response.text!r}")

    result = _parse_responses_body(body, latency_ms)
    _log_responses_usage(payload, result)
    if result.status != "completed":
        logger.warning("[LLM] Responses 응답이 완료되지 않았습니다: status=%s details=%s", result.status, body.get("incomplete_details"))
    return result


class ResponsesClient:
    """실제 /v1/responses 클라이언트. model·reasoning_effort를 생략하면 ASSISTANT_* 설정을 쓴다."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = RESPONSES_TIMEOUT_SECONDS,
    ) -> None:
        self.http_client = http_client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout

    def create(
        self,
        *,
        task: ResponsesTask,
        instruction_blocks: list[str],
        tools: list[FunctionTool],
        input_items: list[ResponsesInputItem],
    ) -> ResponsesResult:
        payload = build_responses_payload(
            task, instruction_blocks, tools, input_items, model=self.model, reasoning_effort=self.reasoning_effort
        )
        return post_responses(payload, self.http_client, timeout=self.timeout)
