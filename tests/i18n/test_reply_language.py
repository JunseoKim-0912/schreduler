"""대화 답변 언어 고르기 (FR-11): 사용자가 마지막에 쓴 말의 언어, 애매하면 화면 언어."""

import pytest

from app.i18n import reply_language


@pytest.mark.parametrize(
    ("text", "screen", "expected"),
    [
        ("오늘 버스가 늦게 와서 못 갔어", "en", "ko"),
        ("the bus was late", "ko", "en"),
        ("Add CSC369 lecture every Tuesday", "ko", "en"),  # 대문자로 시작해도 흔한 영어 단어면 영어
        ("다음 주에만 ECE355 Quiz 2로 바꿔줘", "en", "ko"),  # 영어 이름이 섞여도 문장은 한국어
        ("좋아, 근데 7시로", "en", "ko"),
        ("ok", "ko", "ko"),
        ("좋아", "en", "en"),
        ("네!", "en", "en"),
        ("Bahen Centre", "ko", "ko"),  # 이름만
        ("ECE360", "en", "en"),  # 과목 코드만
        ("12:30", "ko", "ko"),  # 숫자만
        ("", "en", "en"),
        (None, "ja", "ko"),  # 모르는 화면 언어는 기본(ko)
    ],
)
def test_reply_language(text: str | None, screen: str, expected: str) -> None:
    assert reply_language(text, screen) == expected
