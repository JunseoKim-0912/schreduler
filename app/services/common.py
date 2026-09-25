from __future__ import annotations

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
