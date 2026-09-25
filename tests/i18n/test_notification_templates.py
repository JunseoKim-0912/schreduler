import string
from pathlib import Path
from typing import get_args

import pytest

import app.i18n as i18n_module
from app.i18n import (
    SUPPORTED_LANGUAGES,
    NotificationKey,
    load_notification_templates,
    render_notification,
    to_language,
)

I18N_DIR = Path(i18n_module.__file__).parent


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def test_one_template_file_per_supported_language() -> None:
    files = {path.name for path in I18N_DIR.glob("notifications*.json")}

    assert files == {f"notifications_{language}.json" for language in SUPPORTED_LANGUAGES}


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_each_language_file_has_exactly_the_notification_keys(language: str) -> None:
    templates = load_notification_templates(language)

    assert set(templates) == set(get_args(NotificationKey))
    assert all(text.strip() for text in templates.values())


@pytest.mark.parametrize("key", get_args(NotificationKey))
def test_placeholders_match_across_languages(key: str) -> None:
    placeholder_sets = {frozenset(_placeholders(load_notification_templates(lang)[key])) for lang in SUPPORTED_LANGUAGES}

    assert len(placeholder_sets) == 1


def test_render_picks_language_and_fills_params() -> None:
    assert render_notification("event_start.title", "ko", title="수업") == "[Schreduler] 수업"
    assert render_notification("sleep_checkin.title", "en") == "[Schreduler] Sleep check-in"
    assert render_notification("daily_checkin.body", "en") == "How did your day go?"


def test_param_braces_are_not_reinterpreted() -> None:
    assert render_notification("event_start.title", "en", title="{subject}") == "[Schreduler] {subject}"


@pytest.mark.parametrize(("value", "expected"), [("ko", "ko"), ("en", "en"), (None, "ko"), ("ja", "ko")])
def test_unknown_language_falls_back_to_korean(value: str | None, expected: str) -> None:
    assert to_language(value) == expected
    assert render_notification("sleep_checkin.title", value) == load_notification_templates(expected)["sleep_checkin.title"]
