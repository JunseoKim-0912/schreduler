from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PartialUpdate


class LocalizedText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ko: str = Field(min_length=1)
    en: str = Field(min_length=1)


class ExampleLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    situation: str = Field(min_length=1)
    line: str = Field(min_length=1)


class LocalizedExampleLines(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ko: list[ExampleLine]
    en: list[ExampleLine]


class LocalizedLines(BaseModel):
    """언어별 대사 목록 {"ko": [...], "en": [...]}. 비어 있어도 된다."""

    model_config = ConfigDict(extra="forbid")

    ko: list[str] = []
    en: list[str] = []


class PersonaCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Hana",
                    "display_name": {
                        "ko": "하나",
                        "en": "Hana"
                    },
                    "description": {
                        "ko": "상냥하고 친절한 대학생",
                        "en": "Kind and friendly college student"
                    },
                    "example_lines": {
                        "ko": [
                            {
                                "situation": "칭찬",
                                "line": "정말 잘했어요!"
                            }
                        ],
                        "en": [
                            {
                                "situation": "praise",
                                "line": "You did great!"
                            }
                        ]
                    },
                    "backstory": None
                }
            ]
        },
    )

    name: str = Field(min_length=1, max_length=50)
    display_name: LocalizedText
    description: LocalizedText
    example_lines: LocalizedExampleLines | None = None
    backstory: LocalizedText | None = None
    fallback_lines: LocalizedLines | None = None  # 의미 없는 입력에 LLM 없이 답할 대사


class PersonaUpdate(PartialUpdate):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "backstory": {
                        "ko": "심리학과 3학년",
                        "en": "Third-year psychology student"
                    }
                }
            ]
        },
    )

    NULLABLE_FIELDS = frozenset({"example_lines", "backstory", "fallback_lines"})

    display_name: LocalizedText | None = None
    description: LocalizedText | None = None
    example_lines: LocalizedExampleLines | None = None
    backstory: LocalizedText | None = None
    fallback_lines: LocalizedLines | None = None


class PersonaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    display_name: LocalizedText
    description: LocalizedText
    example_lines: LocalizedExampleLines | None
    backstory: LocalizedText | None
    fallback_lines: LocalizedLines | None = None


class UserPersonaSelect(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "persona_name": "Hana"
                }
            ]
        },
    )

    persona_name: str | None  # null이면 선택 해제


class UserPersonaRead(BaseModel):
    user_id: int
    selected_persona: PersonaRead | None


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    # LLM을 부르지 않고 규칙 기반 필터로 답한 턴 (비용 추적용). 그 이유 (keyboard_mash, prompt_injection 등)
    llm_skipped: bool = False
    filter_reason: str | None = None


class PersonaConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    persona_id: str
    context_type: str
    messages: list[ConversationMessage]
    conversation_date: date | None = None
