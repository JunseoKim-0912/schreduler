import string
from typing import get_args

import pytest

from app.i18n import (
    SUPPORTED_LANGUAGES,
    NotificationKey,
    load_notification_templates,
    render_notification,
    to_language,
)


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def test_template_keys_match_notification_key_literal() -> None:
    assert set(load_notification_templates()) == set(get_args(NotificationKey))


@pytest.mark.parametrize("key", get_args(NotificationKey))
def test_every_template_has_all_languages_with_same_placeholders(key: str) -> None:
    translations = load_notification_templates()[key]

    assert set(translations) == set(SUPPORTED_LANGUAGES)
    assert all(text.strip() for text in translations.values())
    assert len({frozenset(_placeholders(text)) for text in translations.values()}) == 1


def test_render_picks_language_and_fills_params() -> None:
    assert render_notification("event_start.title", "ko", title="수업") == "[Schreduler] 수업"
    assert render_notification("sleep_checkin.title", "en") == "[Schreduler] Sleep check-in"


def test_param_braces_are_not_reinterpreted() -> None:
    assert render_notification("event_start.title", "en", title="{subject}") == "[Schreduler] {subject}"


@pytest.mark.parametrize(("value", "expected"), [("ko", "ko"), ("en", "en"), (None, "ko"), ("ja", "ko")])
def test_unknown_language_falls_back_to_korean(value: str | None, expected: str) -> None:
    assert to_language(value) == expected
