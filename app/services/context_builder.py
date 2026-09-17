from __future__ import annotations

from datetime import date as dt_date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import EventInstanceStatus
from app.models.event import Event
from app.models.event_instance import EventInstance
from app.services.llm_client import NON_COMPLIANCE_CATEGORY_LABELS


def _describe_missed_reason(instance: EventInstance) -> str | None:
    if not instance.compliance_reports:
        return None
    report = instance.compliance_reports[-1]  # 가장 최근에 기록된 사유
    label = NON_COMPLIANCE_CATEGORY_LABELS[report.reason_category]
    if report.reason_text:
        return f"{label} ({report.reason_text})"
    return label


def _describe_missed_instance(instance: EventInstance) -> str:
    event = instance.event
    time_range = f"{event.start_time.strftime('%H:%M')}~{event.end_time.strftime('%H:%M')}"
    reason = _describe_missed_reason(instance)
    detail = f"사유: {reason}" if reason else "사유 미기록"
    return f"{event.title} ({time_range}) - {detail}"


def build_daily_checkin_summary(db: Session, user_id: int, target_date: dt_date) -> str:
    """FR-8 "컨텍스트 동적 로딩": 하루치 EventInstance를 통째로 LLM에 넘기지 않는다.

    완료(done)된 이벤트는 개수만 요약하고, 미완료(missed)된 이벤트만 제목·시간·
    [FR-6]에서 기록된 미준수 사유까지 상세히 담아 프롬프트에 쓸 요약 텍스트를
    만든다. 이렇게 하면 "잘 지킨 일정"에는 토큰을 쓰지 않고, 대화가 필요한
    "놓친 일정"에만 컨텍스트를 집중시킬 수 있다.
    """
    stmt = (
        select(EventInstance)
        .join(Event, EventInstance.event_id == Event.id)
        .where(Event.user_id == user_id, EventInstance.date == target_date)
        .order_by(Event.start_time)
    )
    instances = db.execute(stmt).scalars().all()

    total = len(instances)
    done_count = sum(1 for instance in instances if instance.status == EventInstanceStatus.DONE)
    missed = [instance for instance in instances if instance.status == EventInstanceStatus.MISSED]

    lines = [f"오늘 계획한 {total}개 중 {done_count}개 완료."]

    if missed:
        lines.append("놓친 일정:")
        lines.extend(f"- {_describe_missed_instance(instance)}" for instance in missed)

    return "\n".join(lines)
