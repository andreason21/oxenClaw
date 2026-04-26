"""Tests for channels.list / channels.probe — channel-agnostic ChannelRouter path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oxenclaw.channels import ChannelRouter
from oxenclaw.extensions.telegram.channel import TelegramChannel
from oxenclaw.gateway.channels_methods import register_channels_methods
from oxenclaw.gateway.router import Router


@pytest.fixture()
def patched_bot(monkeypatch):  # type: ignore[no-untyped-def]
    def _fake(token: str) -> MagicMock:
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        me = MagicMock()
        me.username = "bot_user"
        me.full_name = "Bot User"
        bot.get_me = AsyncMock(return_value=me)
        return bot

    monkeypatch.setattr("oxenclaw.extensions.telegram.channel.create_bot", _fake)
    return monkeypatch


async def test_list_groups_accounts_by_channel(patched_bot) -> None:  # type: ignore[no-untyped-def]
    cr = ChannelRouter()
    cr.register("telegram", "main", TelegramChannel(token="t", account_id="main"))
    cr.register("telegram", "secondary", TelegramChannel(token="t", account_id="secondary"))

    router = Router()
    register_channels_methods(router, cr)

    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "channels.list"})
    assert resp.result == {"telegram": ["main", "secondary"]}


async def test_list_empty_registry() -> None:
    router = Router()
    register_channels_methods(router, ChannelRouter())
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "channels.list"})
    assert resp.result == {}


async def test_probe_happy(patched_bot) -> None:  # type: ignore[no-untyped-def]
    cr = ChannelRouter()
    cr.register("telegram", "main", TelegramChannel(token="t", account_id="main"))
    router = Router()
    register_channels_methods(router, cr)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "channels.probe",
            "params": {"channel": "telegram", "account_id": "main"},
        }
    )
    assert resp.result["ok"] is True
    assert resp.result["display_name"] == "bot_user"


async def test_probe_unknown_binding() -> None:
    cr = ChannelRouter()
    router = Router()
    register_channels_methods(router, cr)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "channels.probe",
            "params": {"channel": "discord", "account_id": "main"},
        }
    )
    assert resp.result["ok"] is False
    assert "not loaded" in (resp.result.get("error") or "")
