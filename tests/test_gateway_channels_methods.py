"""Tests for channels.list / channels.probe — channel-agnostic ChannelRouter path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oxenclaw.channels import ChannelRouter
from oxenclaw.extensions.slack.channel import SlackChannel
from oxenclaw.gateway.channels_methods import register_channels_methods
from oxenclaw.gateway.router import Router


def _slack_channel(account_id: str) -> SlackChannel:
    client = MagicMock()
    client._call = AsyncMock(return_value={"ok": True, "team": "acme", "user": "bot"})
    client.aclose = AsyncMock()
    return SlackChannel(token="xoxb-test", account_id=account_id, client=client)


@pytest.fixture()
def patched_slack() -> None:
    return None


async def test_list_groups_accounts_by_channel() -> None:
    cr = ChannelRouter()
    cr.register("slack", "main", _slack_channel("main"))
    cr.register("slack", "secondary", _slack_channel("secondary"))

    router = Router()
    register_channels_methods(router, cr)

    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "channels.list"})
    assert resp.result == {"slack": ["main", "secondary"]}


async def test_list_empty_registry() -> None:
    router = Router()
    register_channels_methods(router, ChannelRouter())
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "channels.list"})
    assert resp.result == {}


async def test_probe_happy() -> None:
    cr = ChannelRouter()
    cr.register("slack", "main", _slack_channel("main"))
    router = Router()
    register_channels_methods(router, cr)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "channels.probe",
            "params": {"channel": "slack", "account_id": "main"},
        }
    )
    assert resp.result["ok"] is True
    assert resp.result["display_name"] == "bot@acme"


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
