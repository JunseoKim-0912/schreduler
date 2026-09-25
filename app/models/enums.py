import enum


class Importance(enum.IntEnum):
    """중요도 체계 (docs/기획보고서.md 3절). Event.importance가 NULL이면 '없음'(수면)을 뜻한다."""

    LEISURE = 1
    SOCIAL = 2
    OBLIGATION_NO_CHECK = 3
    OFFICIAL = 4
    MUST = 5
    MAX = 6


class EventType(enum.Enum):
    """SCHEDULED는 start_time~end_time 구간 일정, DEADLINE은 start_time 없이 end_time(마감 일시)만 갖는다."""

    SCHEDULED = "scheduled"
    DEADLINE = "deadline"


class EventInstanceStatus(enum.Enum):
    PENDING = "pending"
    DONE = "done"
    MISSED = "missed"


class CompletionMethod(enum.Enum):
    MANUAL = "manual"
    GPS = "gps"


class NonComplianceCategory(enum.Enum):
    """미준수 사유 카테고리 (docs/기획보고서.md FR-6). 확정안이 아니라 초안."""

    OVERSLEPT = "overslept"
    FATIGUE = "fatigue"
    PRIORITY_SHIFT = "priority_shift"
    SCHEDULE_CONFLICT = "schedule_conflict"
    FORGOT = "forgot"
    TRANSIT_ISSUE = "transit_issue"
    OTHER = "other"


class EngagementScope(enum.Enum):
    """FR-4-1 통합 에스컬레이션 정책의 적용 범위. EVENT는 개별 이벤트 무응답,
    GLOBAL은 앱 전체 미접속을 추적한다."""

    EVENT = "event"
    GLOBAL = "global"


class EscalationStage(enum.Enum):
    """FR-4-1: 1주(텔레그램 opt-in 알림) -> 2주(모든 알림 중단) -> 3주(최종 메시지 후
    완전 중단), 응답 시 즉시 NORMAL로 리셋."""

    NORMAL = "normal"
    WEEK_1_TELEGRAM = "week_1_telegram"
    WEEK_2_MUTED = "week_2_muted"
    WEEK_3_FINAL = "week_3_final"


class ChildEventKind(enum.Enum):
    """Event.parent_event_id가 있을 때, 그 Child가 왜 생겼는지 구분한다 (FR-5).

    TRAVEL은 Location.default_travel_minutes로 자동 생성/재배치되는 이동시간 Child이고,
    CUSTOM은 사용자가 직접 만든 임의의 준비 Child(예: 면접 전 "준비" 30분)다.
    이동시간 체인 재배치(reconcile_missed_event)는 TRAVEL Child만 대상으로 한다.
    """

    TRAVEL = "travel"
    CUSTOM = "custom"
