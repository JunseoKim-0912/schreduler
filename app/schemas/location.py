from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import NonEmptyStr, PartialUpdate


class LocationCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "name": "학교",
                    "default_travel_minutes": 40
                }
            ]
        },
    )

    user_id: int
    name: NonEmptyStr
    default_travel_minutes: int = Field(gt=0)


class LocationUpdate(PartialUpdate):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "default_travel_minutes": 35
                }
            ]
        },
    )

    name: NonEmptyStr | None = None
    default_travel_minutes: int | None = Field(default=None, gt=0)


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    default_travel_minutes: int
