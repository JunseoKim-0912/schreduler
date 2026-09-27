"""페르소나 대화의 의미 없는 입력을 LLM 없이 거르는 규칙 (FR-9 v3.8).

정상 입력을 막는 쪽이 더 나쁜 실패라서 확실한 경우만 거른다. "ㅋㅋ", "응", "힘들어", "hahaha", 영어 문장,
이모지가 섞인 문장처럼 짧거나 감정 표현인 입력은 전부 LLM으로 보낸다.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

FilterReason = Literal["too_long", "prompt_injection", "keyboard_mash", "repeated_char", "symbols_only"]

MAX_LENGTH = 1000

_INJECTION = re.compile(
    r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|messages)"
    r"|disregard\s+(all\s+)?(the\s+)?(previous|prior|above)\s+instructions"
    r"|forget\s+(all\s+)?(your|the)\s+(previous\s+)?instructions"
    r"|(reveal|show|print)\s+(me\s+)?(your|the)\s+system\s+prompt"
    r"|you\s+are\s+now\s+(dan|in\s+developer\s+mode)"
    r"|(이전|앞의|위의|기존)\s*(의\s*)?(모든\s*)?(지시|명령|지침|프롬프트)\S*\s*(을|를)?\s*(모두\s*)?(무시|잊)"
    r"|시스템\s*프롬프트\S*\s*(보여|알려|출력|공개)",
    re.IGNORECASE,
)
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
# 한 글자만 반복해도 흔한 감정 표현인 글자 (ㅋㅋㅋㅋ, ㅠㅠㅠ, 하하하, ..., !!!)
_EXPRESSIVE = set("ㅋㅎㅠㅜㅡㄷㅇ하히호헤흑.!?~;^ㅏzZhH")
_JAMO = re.compile(r"^[ㄱ-ㅎㅏ-ㅣ]+$")


def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) == "So" or 0x1F000 <= ord(ch) <= 0x1FAFF


def _one_row_mash(token: str) -> bool:
    """'asdfasdf', 'qweqwe', 'sdfghjk'처럼 키보드 한 줄을 두드린 영문."""
    if len(token) < 5 or not token.isalpha() or not token.isascii():
        return False
    token = token.lower()
    row = next((r for r in _KEYBOARD_ROWS if set(token) <= set(r)), None)
    if row is None or len(set(token)) < 3:
        return False
    for size in range(3, len(token) // 2 + 1):  # 같은 조각의 반복
        chunk = token[:size]
        if (chunk * (len(token) // size + 1)).startswith(token) and len(token) >= size * 2:
            return True
    return token in row or token in row[::-1]  # 연속한 키 5개 이상 (asdfg, poiuy)


def check(utterance: str) -> FilterReason | None:
    """LLM 없이 처리할 입력이면 그 이유, 아니면 None."""
    text = utterance.strip()
    if len(text) > MAX_LENGTH:
        return "too_long"
    if _INJECTION.search(text):
        return "prompt_injection"
    compact = re.sub(r"\s+", "", text)
    if not compact or any(_is_emoji(ch) for ch in compact):
        return None
    if len(set(compact)) == 1 and len(compact) >= 6 and compact[0] not in _EXPRESSIVE:
        return "repeated_char"
    if _JAMO.match(compact) and len(compact) >= 4 and len(set(compact) - _EXPRESSIVE) >= 3:
        return "keyboard_mash"  # ㅁㄴㅇㄹ, ㅂㅈㄷㄱ
    tokens = text.split()
    if tokens and all(_one_row_mash(token) for token in tokens):
        return "keyboard_mash"
    if not any(ch.isalnum() or "ㄱ" <= ch <= "ㅣ" for ch in compact) and len(set(compact)) >= 4:
        return "symbols_only"  # !@#$%, ...!?는 표현이라 통과
    return None
