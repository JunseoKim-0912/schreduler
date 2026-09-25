from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.enums import CompletionMethod, EventInstanceStatus
from app.models.event_instance import EventInstance
from app.services.points import recalculate_points_since


def complete_event_instance(
    db: Session, instance: EventInstance, method: CompletionMethod = CompletionMethod.MANUAL
) -> EventInstance:
    """EventInstance를 완료(DONE) 처리한다. 이미 완료된 인스턴스는 그대로 둔다.

    지난 날짜의 인스턴스를 뒤늦게 완료하면 자정 잡이 이미 그날 포인트를 기록했으므로, 그 날짜부터
    어제까지의 PointsLedger를 즉시 다시 계산한다. 오늘 날짜분은 /points/summary가 실시간으로 반영한다.
    """
    if instance.status != EventInstanceStatus.DONE:
        instance.status = EventInstanceStatus.DONE
        instance.completion_method = method
        db.commit()
        db.refresh(instance)
        recalculate_points_since(db, instance.event.user_id, instance.date)
    return instance
