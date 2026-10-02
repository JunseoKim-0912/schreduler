from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# Deliberately loose: it only has to look like an address, the account is what matters.
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
# Upper bound only so a huge body can't make argon2 hash megabytes.
NewPassword = Annotated[str, StringConstraints(min_length=8, max_length=256)]


class SignupRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"email": "june@example.com", "password": "correct horse battery", "invite_code": "spring-2026"}]}
    )

    email: Email
    password: NewPassword
    invite_code: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"email": "june@example.com", "password": "correct horse battery"}]})

    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]
    password: Annotated[str, StringConstraints(max_length=256)]


class MeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str | None
    is_admin: bool
    preferred_language: str
    created_at: datetime
    last_login_at: datetime | None
