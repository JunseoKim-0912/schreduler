from __future__ import annotations

from datetime import date as dt_date
from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class SleepLogCreate(BaseModel):
    user_id: int
    date: dt_date
    actual_bedtime: datetime
    actual_wake_time: datetime

    @model_validator(mode="after")
    def check_wake_after_bedtime(self) -> "SleepLogCreate":
        if self.actual_wake_time <= self.actual_bedtime:
            raise ValueError("actual_wake_time must be after actual_bedtime")
        return self


class SleepLogUpdate(BaseModel):
    date: dt_date | None = None
    actual_bedtime: datetime | None = None
    actual_wake_time: datetime | None = None

    @model_validator(mode="after")
    def check_wake_after_bedtime(self) -> "SleepLogUpdate":
        if (
            self.actual_bedtime is not None
            and self.actual_wake_time is not None
            and self.actual_wake_time <= self.actual_bedtime
        ):
            raise ValueError("actual_wake_time must be after actual_bedtime")
        return self


class SleepLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    date: dt_date
    actual_bedtime: datetime
    actual_wake_time: datetime
