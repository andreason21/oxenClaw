"""Tests for TelegramChannel: protocol conformance + plugin-level behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sampyclaw.extensions.telegram.channel import TelegramChannel
from sampyclaw.plugin_sdk.channel_contract import (
    ChannelPlugin,
    ChannelTarget,
    InboundEnvelope,
    MonitorOpts,
    ProbeOpts,
    SendParams,
)
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError


def _patch_bot(monkeypatch, channel: TelegramChannel) -> MagicMock:  # type: ignore[no-untyped-def]
    bot = MagicMock()
    bot.session = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr("sampyclaw.extensions.telegram.channel.create_bot", lambda token: bot)
    return bot


def test_constructor_requires_token() -> None:
    with pytest.raises(ValueError):
        TelegramChannel(token="")


def test_telegram_channel_conforms_to_channel_plugin_protocol() -> None:
    ch = TelegramChannel(token="fake", account_id="main")
    assert isinstance(ch, ChannelPlugin)
    assert ch.id == "telegram"


async def test_send_delegates_and_returns_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = TelegramChannel(token="fake", account_id="main")
    bot = _patch_bot(monkeypatch, ch)

    sent = MagicMock()
    sent.message_id = 7
    sent.date = datetime(2026, 4, 24, tzinfo=UTC)
    bot.send_message = AsyncMock(return_value=sent)

    result = await ch.send(
        SendParams(
            target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
            text="hi",
        )
    )
    assert result.message_id == "7"
    bot.send_message.assert_awaited_once()


async def test_send_rejects_wrong_channel(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = TelegramChannel(token="fake", account_id="main")
    _patch_bot(monkeypatch, ch)
    with pytest.raises(UserVisibleError):
        await ch.send(
            SendParams(
                target=ChannelTarget(channel="discord", account_id="main", chat_id="42"),
                text="hi",
            )
        )


async def test_monitor_rejects_mismatched_account(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = TelegramChannel(token="fake", account_id="main")
    _patch_bot(monkeypatch, ch)

    async def _handler(env: InboundEnvelope) -> None:
        return None

    with pytest.raises(UserVisibleError):
        await ch.monitor(MonitorOpts(account_id="other", on_inbound=_handler))


async def test_probe_ok(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = TelegramChannel(token="fake", account_id="main")
    bot = _patch_bot(monkeypatch, ch)
    me = MagicMock()
    me.username = "my_bot"
    me.full_name = "My Bot"
    bot.get_me = AsyncMock(return_value=me)

    result = await ch.probe(ProbeOpts(account_id="main"))
    assert result.ok is True
    assert result.display_name == "my_bot"


async def test_probe_error_returns_not_ok(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = TelegramChannel(token="fake", account_id="main")
    bot = _patch_bot(monkeypatch, ch)
    bot.get_me = AsyncMock(side_effect=RuntimeError("network down"))
    result = await ch.probe(ProbeOpts(account_id="main"))
    assert result.ok is False
    assert "network down" in (result.error or "")


async def test_aclose_closes_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = TelegramChannel(token="fake", account_id="main")
    bot = _patch_bot(monkeypatch, ch)

    # Force bot creation via probe so there's a session to close.
    bot.get_me = AsyncMock(return_value=MagicMock(username="x", full_name="X"))
    await ch.probe(ProbeOpts(account_id="main"))

    await ch.aclose()
    bot.session.close.assert_awaited_once()
