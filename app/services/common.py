from __future__ import annotations

import re
from typing import TypeVar

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError

ModelT = TypeVar("ModelT")


def require(db: Session, model: type[ModelT], pk: object, label: str) -> ModelT:
    """pk로 조회하고, 없으면 "{label} {pk} does not exist" NotFoundError(404)를 던진다."""
    obj = db.get(model, pk)
    if obj is None:
        raise NotFoundError(f"{label} {pk} does not exist")
    return obj


_PARENTHESIZED = re.compile(r"[(（][^)）]*[)）]")


def name_key(name: str) -> str:
    """이름 비교용 키: 괄호 안 설명("Bahen Centre (이동 10분)"), 대소문자, 공백을 무시한다. 장소·반복 기간 매칭에 쓴다."""
    return re.sub(r"\s+", "", _PARENTHESIZED.sub("", name)).casefold()
