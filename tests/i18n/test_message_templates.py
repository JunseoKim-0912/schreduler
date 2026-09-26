import string
from typing import get_args

import pytest

from app.i18n import SUPPORTED_LANGUAGES, MessageKey, load_message_templates, render_message


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_each_language_file_has_exactly_the_message_keys(language: str) -> None:
    assert set(load_message_templates(language)) == set(get_args(MessageKey))


@pytest.mark.parametrize("key", get_args(MessageKey))
def test_placeholders_match_across_languages(key: str) -> None:
    placeholders = {frozenset(_placeholders(load_message_templates(lang)[key])) for lang in SUPPORTED_LANGUAGES}
    assert len(placeholders) == 1


def test_render_message_by_language() -> None:
    assert render_message("command.not_found", "ko", title="물리 퀴즈") == "'물리 퀴즈'에 해당하는 일정을 찾지 못했어요."
    assert render_message("command.not_found", "en", title="Quiz") == "I couldn't find an event matching 'Quiz'."
