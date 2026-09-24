from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Literal

Language = Literal["ko", "en"]
SUPPORTED_LANGUAGES: tuple[Language, ...] = ("ko", "en")
DEFAULT_LANGUAGE: Language = "ko"

NotificationKey = Literal[
    "event_start.title",
    "event_start.body",
    "event_end.title",
    "event_end.body",
    "escalation.subject_event",
    "escalation.subject_app",
    "escalation.week_1",
    "escalation.week_3",
    "sleep_checkin.title",
    "sleep_checkin.body",
]

_NOTIFICATIONS_PATH = Path(__file__).with_name("notifications.json")


@cache
def load_notification_templates() -> dict[str, dict[str, str]]:
    return json.loads(_NOTIFICATIONS_PATH.read_text(encoding="utf-8"))


def to_language(value: str | None) -> Language:
    return "en" if value == "en" else DEFAULT_LANGUAGE


def render_notification(key: NotificationKey, language: str | None, **params: str) -> str:
    """알림 문구 템플릿을 사용자 언어로 골라 params를 채운다 (FR-11).

    params 값(일정 제목 등) 안의 중괄호는 다시 해석되지 않는다.
    """
    template = load_notification_templates()[key][to_language(language)]
    return template.format(**params)
