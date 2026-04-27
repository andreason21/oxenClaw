"""Tests for ChannelRouter: register / send / probe / bindings / aclose."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oxenclaw.channels import ChannelRouter
from oxenclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)


def _mock_channel(channel_id: str = "dashboard") -> MagicMock:
    ch = MagicMock()
    ch.id = channel_id
    ch.send = AsyncMock(return_value=SendResult(message_id="m1", timestamp=1.0))
    ch.probe = AsyncMock(return_value=ProbeResult(ok=True, account_id="main", display_name="bot"))
    ch.aclose = AsyncMock()
    return ch


def _params() -> SendParams:
    return SendParams(
        target=ChannelTarget(channel="dashboard", account_id="main", chat_id="42"),
        text="hi",
    )


async def test_register_and_get() -> None:
    cr = ChannelRouter()
    ch = _mock_channel()
    cr.register("dashboard", "main", ch)
    assert cr.get("dashboard", "main") is ch
    assert cr.get("dashboard", "other") is None


async def test_register_duplicate_raises() -> None:
    cr = ChannelRouter()
    cr.register("dashboard", "main", _mock_channel())
    with pytest.raises(ValueError):
        cr.register("dashboard", "main", _mock_channel())


async def test_require_missing_raises() -> None:
    cr = ChannelRouter()
    from oxenclaw.plugin_sdk.error_runtime import UserVisibleError

    with pytest.raises(UserVisibleError):
        cr.require("dashboard", "none")


async def test_channels_by_id_groups_and_sorts() -> None:
    cr = ChannelRouter()
    cr.register("dashboard", "b", _mock_channel())
    cr.register("dashboard", "a", _mock_channel())
    cr.register("discord", "main", _mock_channel("discord"))
    assert cr.channels_by_id() == {
        "dashboard": ["a", "b"],
        "discord": ["main"],
    }


async def test_send_routes_to_matching_channel() -> None:
    cr = ChannelRouter()
    ch = _mock_channel()
    cr.register("dashboard", "main", ch)
    result = await cr.send(_params())
    assert result.message_id == "m1"
    ch.send.assert_awaited_once()


async def test_send_unrouted_raises_user_visible() -> None:
    """Sending to an unbound channel must raise (not silently return a fake
    success), so RPC clients see the routing misconfig."""
    import pytest

    from oxenclaw.plugin_sdk.error_runtime import UserVisibleError

    cr = ChannelRouter()
    with pytest.raises(UserVisibleError, match="no channel plugin"):
        await cr.send(_params())


async def test_probe_calls_channel_with_opts() -> None:
    cr = ChannelRouter()
    ch = _mock_channel()
    cr.register("dashboard", "main", ch)
    result = await cr.probe("dashboard", "main")
    assert result.ok is True
    ch.probe.assert_awaited_once()
    assert isinstance(ch.probe.call_args.args[0], ProbeOpts)


async def test_probe_missing_binding_returns_not_ok() -> None:
    cr = ChannelRouter()
    result = await cr.probe("dashboard", "missing")
    assert result.ok is False
    assert "not loaded" in (result.error or "")


async def test_aclose_invokes_plugin_aclose() -> None:
    cr = ChannelRouter()
    ch = _mock_channel()
    cr.register("dashboard", "main", ch)
    await cr.aclose()
    ch.aclose.assert_awaited_once()
    assert len(cr) == 0


async def test_aclose_swallows_plugin_errors() -> None:
    cr = ChannelRouter()
    ch = _mock_channel()
    ch.aclose = AsyncMock(side_effect=RuntimeError("close broken"))
    cr.register("dashboard", "main", ch)
    await cr.aclose()  # must not raise
    assert len(cr) == 0


async def test_bindings_iterates_all() -> None:
    cr = ChannelRouter()
    cr.register("dashboard", "a", _mock_channel())
    cr.register("discord", "b", _mock_channel("discord"))
    seen = {(c, a) for c, a, _ in cr.bindings()}
    assert seen == {("dashboard", "a"), ("discord", "b")}
