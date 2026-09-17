from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LocationCreate(BaseModel):
    user_id: int
    name: str
    default_travel_minutes: int = Field(gt=0)


class LocationUpdate(BaseModel):
    name: str | None = None
    default_travel_minutes: int | None = Field(default=None, gt=0)


class LocationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    default_travel_minutes: int
