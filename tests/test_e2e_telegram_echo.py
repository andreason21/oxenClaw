"""E2E: Telegram inbound → TelegramPollingSession → Dispatcher → EchoAgent → TelegramChannel.send → mocked bot.send_message.

Proves the whole plumbing is wired up end-to-end without hitting the network.
The aiogram Bot is the only mocked seam; every oxenclaw layer is real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from oxenclaw.agents import AgentRegistry, Dispatcher, EchoAgent
from oxenclaw.extensions.telegram.channel import TelegramChannel
from oxenclaw.extensions.telegram.monitor import TelegramPollingSession
from oxenclaw.plugin_sdk.config_schema import (
    AgentChannelRouting,
    AgentConfig,
    RootConfig,
)


def _build_inbound_message(text: str, *, chat_id: int = 42, sender_id: int = 100):  # type: ignore[no-untyped-def]
    from aiogram.types import Chat, Message, User

    return Message.model_validate(
        {
            "message_id": 1,
            "date": int(datetime(2026, 4, 24, tzinfo=UTC).timestamp()),
            "chat": Chat(id=chat_id, type="private").model_dump(),
            "from": User(id=sender_id, is_bot=False, first_name="Alice").model_dump(),
            "text": text,
            "message_thread_id": None,
        }
    )


def _build_mock_bot():  # type: ignore[no-untyped-def]
    bot = MagicMock()
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    sent_result = MagicMock()
    sent_result.message_id = 999
    sent_result.date = datetime(2026, 4, 24, tzinfo=UTC)
    bot.send_message = AsyncMock(return_value=sent_result)
    return bot


async def test_inbound_text_produces_echo_send(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bot = _build_mock_bot()
    monkeypatch.setattr("oxenclaw.extensions.telegram.channel.create_bot", lambda token: bot)

    channel = TelegramChannel(token="fake", account_id="main")
    agents = AgentRegistry()
    agents.register(EchoAgent())
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=[])},
            )
        }
    )
    dispatcher = Dispatcher(agents=agents, config=config, send=channel.send)

    session = TelegramPollingSession(bot=bot, account_id="main", on_inbound=dispatcher.dispatch)

    await session._handle_message(_build_inbound_message("hello world"))

    bot.send_message.assert_awaited_once()
    call = bot.send_message.call_args.kwargs
    assert call["chat_id"] == 42
    assert call["text"] == "echo: hello world"


async def test_inbound_from_unauthorized_sender_is_dropped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bot = _build_mock_bot()
    monkeypatch.setattr("oxenclaw.extensions.telegram.channel.create_bot", lambda token: bot)

    channel = TelegramChannel(token="fake", account_id="main")
    agents = AgentRegistry()
    agents.register(EchoAgent())
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=["200"])},
            )
        }
    )
    dispatcher = Dispatcher(agents=agents, config=config, send=channel.send)
    session = TelegramPollingSession(bot=bot, account_id="main", on_inbound=dispatcher.dispatch)

    await session._handle_message(_build_inbound_message("hi", sender_id=100))

    bot.send_message.assert_not_called()


async def test_multiple_inbound_each_produces_echo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bot = _build_mock_bot()
    monkeypatch.setattr("oxenclaw.extensions.telegram.channel.create_bot", lambda token: bot)

    channel = TelegramChannel(token="fake", account_id="main")
    agents = AgentRegistry()
    agents.register(EchoAgent())
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=[])},
            )
        }
    )
    dispatcher = Dispatcher(agents=agents, config=config, send=channel.send)
    session = TelegramPollingSession(bot=bot, account_id="main", on_inbound=dispatcher.dispatch)

    for i, text in enumerate(["first", "second", "third"], start=1):
        msg = _build_inbound_message(text).model_copy(update={"message_id": i})
        await session._handle_message(msg)

    assert bot.send_message.await_count == 3
    replies = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert replies == ["echo: first", "echo: second", "echo: third"]
