"""Tests for Telegram send — text happy path + error taxonomy mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sampyclaw.extensions.telegram.send import send_message_telegram
from sampyclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    SendParams,
)
from sampyclaw.plugin_sdk.error_runtime import (
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)


def _params(**overrides) -> SendParams:  # type: ignore[no-untyped-def]
    base = dict(
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        text="hi",
    )
    base.update(overrides)
    return SendParams(**base)  # type: ignore[arg-type]


def _fake_sent_message(message_id: int = 123) -> MagicMock:
    m = MagicMock()
    m.message_id = message_id
    m.date = datetime(2026, 4, 24, tzinfo=UTC)
    return m


async def test_send_text_roundtrip() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=_fake_sent_message(message_id=7))
    result = await send_message_telegram(bot, _params())
    assert result.message_id == "7"
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["text"] == "hi"
    assert kwargs["message_thread_id"] is None
    assert kwargs["reply_to_message_id"] is None


async def test_send_passes_thread_id_and_reply() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=_fake_sent_message())
    await send_message_telegram(
        bot,
        _params(
            target=ChannelTarget(
                channel="telegram",
                account_id="main",
                chat_id="42",
                thread_id="777",
            ),
            reply_to_message_id="555",
        ),
    )
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 777
    assert kwargs["reply_to_message_id"] == 555


async def test_send_rejects_empty_text() -> None:
    bot = MagicMock()
    with pytest.raises(ValueError):
        await send_message_telegram(bot, _params(text=""))


async def test_send_maps_retry_after_to_rate_limited() -> None:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    method = SendMessage(chat_id=42, text="hi")

    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramRetryAfter(method=method, message="slow", retry_after=5)
    )
    with pytest.raises(RateLimitedError) as excinfo:
        await send_message_telegram(bot, _params())
    assert excinfo.value.retry_after == 5.0


async def test_send_maps_network_error() -> None:
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    method = SendMessage(chat_id=42, text="hi")
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=TelegramNetworkError(method=method, message="boom"))
    with pytest.raises(NetworkError):
        await send_message_telegram(bot, _params())


async def test_send_maps_bad_request_to_user_visible() -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    method = SendMessage(chat_id=42, text="hi")
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=TelegramBadRequest(method=method, message="chat not found")
    )
    with pytest.raises(UserVisibleError):
        await send_message_telegram(bot, _params())
