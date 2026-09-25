from __future__ import annotations

from datetime import datetime
from typing import Annotated, ClassVar, Self

from dateutil.rrule import rrulestr
from pydantic import AfterValidator, BaseModel, StringConstraints, model_validator

# 앞뒤 공백을 지우고, 빈 문자열("", "   ")은 422로 거부한다.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _check_recurrence_rule(value: str) -> str:
    # 저장 전에 한 번 파싱해 본다. 여기서 막지 않으면 잘못된 규칙이 저장된 뒤 인스턴스 생성 시점에 터진다.
    try:
        rrulestr(value, dtstart=datetime(2000, 1, 1))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid recurrence_rule {value!r}: {exc}") from exc
    return value


# RFC 5545 RRULE 문자열 (예: "FREQ=WEEKLY;BYDAY=TU")
RecurrenceRule = Annotated[str, AfterValidator(_check_recurrence_rule)]


class PartialUpdate(BaseModel):
    """보낸 필드만 수정하는 *Update 스키마의 베이스.

    필드를 생략하면 "안 바꿈"이고, null을 명시하면 "값을 지움"이다. NULLABLE_FIELDS에 없는 필드(DB에서
    NOT NULL인 필드)에 null을 보내면 DB 제약 위반까지 가지 않도록 여기서 422로 막는다.
    """

    NULLABLE_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def reject_null_for_required_fields(self) -> Self:
        for field in sorted(self.model_fields_set - self.NULLABLE_FIELDS):
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self
