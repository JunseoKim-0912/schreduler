from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

from app.models.enums import ActionSource, ActionType


class ActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action_type: ActionType
    source: ActionSource
    summary_text: str
    affected_ids: dict[str, Any]
    created_at: datetime
    undone_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def undone(self) -> bool:
        return self.undone_at is not None
