from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Literal

from app.models.enums import NonComplianceCategory

Language = Literal["ko", "en"]
SUPPORTED_LANGUAGES: tuple[Language, ...] = ("ko", "en")
DEFAULT_LANGUAGE: Language = "ko"

NotificationKey = Literal[
    "event_start.title",
    "event_start.body",
    "event_end.title",
    "event_end.body",
    "deadline_reminder.title",
    "deadline_reminder.body",
    "escalation.subject_event",
    "escalation.subject_app",
    "escalation.week_1",
    "escalation.week_3",
    "sleep_checkin.title",
    "sleep_checkin.body",
    "daily_checkin.title",
    "daily_checkin.body",
]

_I18N_DIR = Path(__file__).parent


def to_language(value: str | None) -> Language:
    return "en" if value == "en" else DEFAULT_LANGUAGE


@cache
def load_notification_templates(language: Language) -> dict[str, str]:
    """푸시/텔레그램 알림 문구. 언어마다 notifications_<language>.json 한 파일씩 둔다."""
    path = _I18N_DIR / f"notifications_{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def render_notification(key: NotificationKey, language: str | None, **params: str) -> str:
    """알림 문구 템플릿을 사용자 언어로 골라 params를 채운다 (FR-11).

    params 값(일정 제목 등) 안의 중괄호는 다시 해석되지 않는다.
    """
    template = load_notification_templates(to_language(language))[key]
    return template.format(**params)


_CATEGORIES_PATH = _I18N_DIR / "categories.json"


@cache
def load_non_compliance_category_labels() -> dict[str, dict[str, str]]:
    return json.loads(_CATEGORIES_PATH.read_text(encoding="utf-8"))["non_compliance_category"]


def non_compliance_category_label(category: NonComplianceCategory, language: str | None) -> str:
    """FR-6 카테고리의 화면 표시 라벨. 코드값(category.value)은 그대로 두고 라벨만 언어별로 고른다."""
    return load_non_compliance_category_labels()[category.value][to_language(language)]


MessageKey = Literal[
    "command.executed",
    "summary.create",
    "summary.delete_series",
    "summary.delete_instance",
    "summary.update_series",
    "summary.update_instance",
    "summary.detach_instance",
    "change.event_type",
    "event_type.scheduled",
    "event_type.deadline",
    "summary.multiple",
    "summary.action.delete",
    "summary.action.update",
    "change.time",
    "change.deadline",
    "change.title",
    "change.importance",
    "undo.newer_change_exists",
    "undo.already_undone",
    "summary.create_with_range",
    "summary.range_create",
    "summary.range_update",
    "summary.range_delete",
    "summary.range_delete_with_events",
    "change.range_dates",
    "change.range_name",
    "change.range_instances",
    "undo.range_in_use",
    "change.location",
    "change.no_location",
    "persona.fallback_default",
    "time.clock_am",
    "time.clock_pm",
    "time.range",
    "time.range_next_day",
    "time.range_days_later",
    "time.deadline",
    "time.hours",
    "time.minutes",
    "time.hours_minutes",
    "assistant.warning.crosses_midnight",
    "assistant.warning.over_12_hours",
    "assistant.warning.past_date",
    "assistant.warning.start_weekday_mismatch",
    "assistant.warning.multiple_targets",
    "assistant.warning.range_in_use",
    "assistant.warning.similar_exists",
    "assistant.proposal_ready",
    "assistant.limit_with_drafts",
    "assistant.limit_no_drafts",
    "assistant.empty_reply",
    "assistant.cancelled",
]


@cache
def load_message_templates(language: Language) -> dict[str, str]:
    """API 응답 문구(자연어 일정 관리 안내, 변경 기록 요약 등). 언어마다 messages_<language>.json."""
    path = _I18N_DIR / f"messages_{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def render_message(key: MessageKey, language: str | None, **params: object) -> str:
    return load_message_templates(to_language(language))[key].format(**params)
