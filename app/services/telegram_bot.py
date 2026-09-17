from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

from app.core.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)


def _get_bot() -> Bot | None:
    if not settings.telegram_bot_token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN이 설정되지 않아 텔레그램 발송을 건너뜁니다 (.env 확인)"
        )
        return None
    return Bot(token=settings.telegram_bot_token)


def is_telegram_opt_in(user: User) -> bool:
    """FR-4-1 에스컬레이션 알림은 텔레그램 opt-in한 사용자에게만 보낼 수 있다.

    opt-in 플래그만 켜져 있고 아직 봇과 대화를 시작하지 않아 chat_id가 없는
    경우도 "보낼 수 없음"으로 취급한다.
    """
    return bool(user.telegram_opt_in and user.telegram_chat_id)


def send_telegram_message(user: User, text: str) -> None:
    """user에게 텔레그램 메시지를 보낸다.

    opt-in하지 않았거나 chat_id가 없으면 실제 호출 없이 로그만 남기고, 봇 토큰이
    없거나 발송이 실패해도 예외를 올리지 않는다 (알림 실패가 호출부 흐름을
    깨면 안 되므로) — send_push_notification과 동일한 원칙.
    """
    if not is_telegram_opt_in(user):
        logger.info(
            "[텔레그램 발송 생략] user_id=%s opt_in=%s chat_id=%s (opt-in 안 함)",
            user.id,
            user.telegram_opt_in,
            user.telegram_chat_id,
        )
        return

    bot = _get_bot()
    if bot is None:
        return

    try:
        asyncio.run(bot.send_message(chat_id=user.telegram_chat_id, text=text))
    except TelegramError as exc:
        logger.warning("[텔레그램 발송 실패] user_id=%s error=%s", user.id, exc)
        return

    logger.info("[텔레그램 발송 성공] user_id=%s", user.id)
