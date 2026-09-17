import pytest

from app.core.config import settings
from app.models.user import User
from app.services import telegram_bot as telegram_bot_module
from app.services.telegram_bot import is_telegram_opt_in, send_telegram_message


@pytest.fixture(autouse=True)
def bot_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")


def _user(*, opt_in: bool, chat_id: str | None) -> User:
    return User(
        id=1,
        name="June",
        preferred_language="ko",
        telegram_opt_in=opt_in,
        telegram_chat_id=chat_id,
    )


def test_is_telegram_opt_in_requires_both_flag_and_chat_id() -> None:
    assert is_telegram_opt_in(_user(opt_in=True, chat_id="12345")) is True
    assert is_telegram_opt_in(_user(opt_in=False, chat_id="12345")) is False
    assert is_telegram_opt_in(_user(opt_in=True, chat_id=None)) is False
    assert is_telegram_opt_in(_user(opt_in=False, chat_id=None)) is False


def test_send_telegram_message_skips_when_not_opted_in(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    called = False

    def _fake_get_bot():
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(telegram_bot_module, "_get_bot", _fake_get_bot)

    with caplog.at_level("INFO", logger="app.services.telegram_bot"):
        send_telegram_message(_user(opt_in=False, chat_id=None), "안녕하세요")

    assert called is False
    assert any("발송 생략" in record.message for record in caplog.records)


def test_send_telegram_message_skips_when_bot_token_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "telegram_bot_token", None)

    with caplog.at_level("WARNING", logger="app.services.telegram_bot"):
        send_telegram_message(_user(opt_in=True, chat_id="12345"), "안녕하세요")

    assert any("TELEGRAM_BOT_TOKEN" in record.message for record in caplog.records)


def test_send_telegram_message_logs_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _FakeBot:
        async def send_message(self, chat_id, text):
            return object()

    monkeypatch.setattr(telegram_bot_module, "_get_bot", lambda: _FakeBot())

    with caplog.at_level("INFO", logger="app.services.telegram_bot"):
        send_telegram_message(_user(opt_in=True, chat_id="12345"), "안녕하세요")

    assert any("발송 성공" in record.message for record in caplog.records)


def test_send_telegram_message_logs_warning_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from telegram.error import TelegramError

    class _FakeBot:
        async def send_message(self, chat_id, text):
            raise TelegramError("chat not found")

    monkeypatch.setattr(telegram_bot_module, "_get_bot", lambda: _FakeBot())

    with caplog.at_level("WARNING", logger="app.services.telegram_bot"):
        send_telegram_message(_user(opt_in=True, chat_id="12345"), "안녕하세요")

    assert any("발송 실패" in record.message for record in caplog.records)
