from __future__ import annotations

import json
import re
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


# 언어를 판단할 근거가 못 되는 짧은 대답. 이런 입력에는 화면 언어로 답한다.
_ACKNOWLEDGEMENTS = {
    "ok", "okay", "k", "kk", "yes", "yeah", "yep", "no", "nope", "sure", "thanks", "thank you", "thx", "cool", "great", "fine",
    "좋아", "좋아요", "응", "웅", "네", "넹", "예", "아니", "아니요", "아뇨", "그래", "그래요", "알겠어", "알겠어요", "고마워", "고마워요",
    "감사", "감사합니다", "ㅇㅋ", "ㅇㅇ", "ㄱㄱ", "오케이",
}
# 대문자로 시작해도 이름이 아니라 영어 문장의 근거가 되는 흔한 단어 (문장 첫 단어 "Add", "Delete" 등).
_COMMON_ENGLISH = {
    "a", "add", "after", "all", "an", "and", "are", "at", "before", "can", "cancel", "change", "could", "create", "delete",
    "do", "every", "for", "from", "how", "i", "i'm", "is", "it", "just", "make", "move", "my", "next", "on", "please",
    "put", "remove", "schedule", "set", "show", "the", "this", "to", "today", "tomorrow", "what", "when", "where", "why",
    "will", "with", "would", "you", "hi", "hello", "hey",
}
_HANGUL = re.compile(r"[가-힣]")
_LATIN_WORD = re.compile(r"[A-Za-z']+")


def reply_language(text: str | None, screen_language: str | None) -> Language:
    """대화 답변 언어: 사용자가 마지막에 입력한 말의 언어를 따른다. 판단하기 애매한 입력("ok", "좋아", 이름·과목 코드·
    숫자만 있는 입력)은 화면 언어(preferred_language)를 따른다. 카드·경고·알림 같은 고정 문구는 이 함수와 상관없이
    항상 화면 언어다."""
    fallback = to_language(screen_language)
    if not text:
        return fallback
    stripped = re.sub(r"[\s.!?~,]+$", "", text.strip()).casefold()
    if stripped in _ACKNOWLEDGEMENTS:
        return fallback
    hangul = len(_HANGUL.findall(text))
    # 소문자로 시작하는 단어와 흔한 영어 단어만 영어의 근거로 센다 — "Bahen Centre", "ECE360 Lab" 같은 이름·코드는 빼고.
    english = [w for w in _LATIN_WORD.findall(text) if w[0].islower() or w.casefold() in _COMMON_ENGLISH]
    english_letters = sum(len(w) for w in english)
    if hangul >= 2 and english_letters == 0:
        return "ko"
    if english_letters >= 2 and hangul == 0:
        return "en"
    if hangul and english_letters:
        # 한글 한 글자는 영어 2~3글자 정도의 정보량이라 2배로 센다.
        return "ko" if hangul * 2 >= english_letters else "en"
    return fallback


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
    "checkin.summary.total",
    "checkin.summary.missed",
    "checkin.summary.due",
    "checkin.summary.reason",
    "checkin.summary.no_reason",
    "child.travel_title",
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
