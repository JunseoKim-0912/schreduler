from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models.assistant import AssistantSession, PendingProposal
from app.models.user import User

DraftKind = Literal["create_event", "update_event", "delete_event", "create_range", "update_range", "delete_range"]


@dataclass
class Draft:
    """검증을 통과한 초안 하나. card는 화면·LLM에 보여줄 값, payload는 확정할 때 실행할 값."""

    draft_id: str
    kind: DraftKind
    card: dict[str, Any]
    payload: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"draft_id": self.draft_id, "kind": self.kind, "card": self.card, "payload": self.payload}


@dataclass
class TurnContext:
    """한 턴 동안 도구들이 함께 보는 상태."""

    db: Session
    user: User
    session: AssistantSession
    now: datetime  # 앱 시간대 aware
    pending: PendingProposal | None = None  # 이전 턴에 보여준 대기 중인 제안
    drafts: dict[str, Draft] = field(default_factory=dict)
    executed: list[dict[str, Any]] = field(default_factory=list)
    _draft_seq: int = 0

    @property
    def today(self) -> date:
        return self.now.date()

    @property
    def wall_now(self) -> datetime:
        """DB에 저장하는 naive 시각 (앱 시간대 기준)."""
        return self.now.replace(tzinfo=None)

    @property
    def turn_started_at(self) -> datetime:
        return self.wall_now

    @property
    def language(self) -> str:
        return self.user.preferred_language

    def next_draft_id(self) -> str:
        self._draft_seq += 1
        return f"d{self._draft_seq}"

    def remember_ids(self, event_ids: list[int], instance_ids: list[int]) -> None:
        seen = self.session.seen_ids or {}
        events = sorted(set(seen.get("events", [])) | set(event_ids))
        instances = sorted(set(seen.get("instances", [])) | set(instance_ids))
        # JSON 컬럼은 새 객체를 넣어야 변경이 감지된다.
        self.session.seen_ids = {"events": events, "instances": instances}

    def seen_event(self, event_id: int) -> bool:
        return event_id in (self.session.seen_ids or {}).get("events", [])

    def seen_instance(self, instance_id: int) -> bool:
        return instance_id in (self.session.seen_ids or {}).get("instances", [])
