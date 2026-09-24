from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class PersonaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    display_name: LocalizedText
    description: LocalizedText
    example_lines: LocalizedExampleLines | None = None
    backstory: LocalizedText | None = None


class PersonaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: LocalizedText | None = None
    description: LocalizedText | None = None
    example_lines: LocalizedExampleLines | None = None
    backstory: LocalizedText | None = None

    @model_validator(mode="after")
    def check_required_fields_not_null(self) -> "PersonaUpdate":
        for field in ("display_name", "description"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PersonaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    display_name: LocalizedText
    description: LocalizedText
    example_lines: LocalizedExampleLines | None
    backstory: LocalizedText | None


class UserPersonaSelect(BaseModel):
    persona_name: str | None  # null이면 선택 해제


class UserPersonaRead(BaseModel):
    user_id: int
    selected_persona: PersonaRead | None


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class PersonaConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    persona_id: str
    context_type: str
    messages: list[ConversationMessage]
