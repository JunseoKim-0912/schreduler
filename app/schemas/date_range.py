from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, model_validator


class DateRangeCreate(BaseModel):
    user_id: int
    name: str
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def check_date_range(self) -> "DateRangeCreate":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class DateRangeUpdate(BaseModel):
    name: str | None = None
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def check_date_range(self) -> "DateRangeUpdate":
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must not be before start_date")
        return self


class DateRangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    start_date: date
    end_date: date
