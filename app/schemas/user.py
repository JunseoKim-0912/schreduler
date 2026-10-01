from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.i18n import Language


class UserLanguageUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"language": "en"}]})

    language: Language


class UserLanguageRead(BaseModel):
    user_id: int
    language: Language
