import enum


class Importance(enum.IntEnum):
    """중요도 체계 (docs/기획보고서.md 3절). Event.importance가 NULL이면 '없음'(수면)을 뜻한다."""

    LEISURE = 1
    SOCIAL = 2
    OBLIGATION_NO_CHECK = 3
    OFFICIAL = 4
    MUST = 5
    MAX = 6


class EventInstanceStatus(enum.Enum):
    PENDING = "pending"
    DONE = "done"
    MISSED = "missed"


class CompletionMethod(enum.Enum):
    MANUAL = "manual"
    GPS = "gps"


class ChildEventKind(enum.Enum):
    """Event.parent_event_id가 있을 때, 그 Child가 왜 생겼는지 구분한다 (FR-5).

    TRAVEL은 Location.default_travel_minutes로 자동 생성/재배치되는 이동시간 Child이고,
    CUSTOM은 사용자가 직접 만든 임의의 준비 Child(예: 면접 전 "준비" 30분)다.
    이동시간 체인 재배치(reconcile_missed_event)는 TRAVEL Child만 대상으로 한다.
    """

    TRAVEL = "travel"
    CUSTOM = "custom"
