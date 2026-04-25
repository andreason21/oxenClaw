"""Tests for TelegramPollingSession: inbound dedup + envelope delivery.

Exercises the message-handling path without running a real polling loop
by invoking `_handle_message` directly with constructed Message objects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sampyclaw.extensions.telegram.bot_core import UpdateDeduplicator
from sampyclaw.extensions.telegram.monitor import TelegramPollingSession
from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope


def _msg(message_id: int, text: str | None = "hi"):  # type: ignore[no-untyped-def]
    from aiogram.types import Chat, Message, User

    return Message.model_validate(
        {
            "message_id": message_id,
            "date": int(datetime(2026, 4, 24, tzinfo=UTC).timestamp()),
            "chat": Chat(id=42, type="private").model_dump(),
            "from": User(id=100, is_bot=False, first_name="Alice").model_dump(),
            "text": text,
            "message_thread_id": None,
        }
    )


async def test_handler_delivers_envelope() -> None:
    received: list[InboundEnvelope] = []

    async def handler(env: InboundEnvelope) -> None:
        received.append(env)

    session = TelegramPollingSession(
        bot=MagicMock(), account_id="main", on_inbound=handler
    )
    await session._handle_message(_msg(1, "hello"))
    assert len(received) == 1
    assert received[0].text == "hello"
    assert received[0].account_id == "main"


async def test_handler_dedups_repeat_message_id() -> None:
    calls = 0

    async def handler(env: InboundEnvelope) -> None:
        nonlocal calls
        calls += 1

    session = TelegramPollingSession(
        bot=MagicMock(), account_id="main", on_inbound=handler
    )
    await session._handle_message(_msg(1))
    await session._handle_message(_msg(1))
    assert calls == 1


async def test_handler_skips_messages_without_text() -> None:
    received: list[InboundEnvelope] = []

    async def handler(env: InboundEnvelope) -> None:
        received.append(env)

    session = TelegramPollingSession(
        bot=MagicMock(), account_id="main", on_inbound=handler
    )
    await session._handle_message(_msg(1, text=None))
    assert received == []


async def test_handler_uses_supplied_deduplicator() -> None:
    dedup = UpdateDeduplicator()
    dedup.seen(1)  # pre-seed

    calls = 0

    async def handler(env: InboundEnvelope) -> None:
        nonlocal calls
        calls += 1

    session = TelegramPollingSession(
        bot=MagicMock(), account_id="main", on_inbound=handler, dedup=dedup
    )
    await session._handle_message(_msg(1))
    assert calls == 0


async def test_handler_swallows_exceptions_from_inbound() -> None:
    async def handler(env: InboundEnvelope) -> None:
        raise RuntimeError("consumer broke")

    session = TelegramPollingSession(
        bot=MagicMock(), account_id="main", on_inbound=handler
    )
    # must not raise out of the polling handler — gets logged instead
    await session._handle_message(_msg(1))
