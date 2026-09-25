from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.enums import CompletionMethod, EventInstanceStatus
from app.models.event_instance import EventInstance


def complete_event_instance(
    db: Session, instance: EventInstance, method: CompletionMethod = CompletionMethod.MANUAL
) -> EventInstance:
    """EventInstance를 완료(DONE) 처리한다. 이미 완료된 인스턴스는 그대로 둔다.

    포인트는 이 상태를 기준으로 계산되므로(app/services/points.py) 별도 적립 호출은 없다 —
    /points/summary에는 즉시, PointsLedger에는 자정 잡에서 반영된다.
    """
    if instance.status != EventInstanceStatus.DONE:
        instance.status = EventInstanceStatus.DONE
        instance.completion_method = method
        db.commit()
        db.refresh(instance)
    return instance
