"""Tests for the bundled Slack outbound-only channel.

The wire seam is `SlackWebClient._call`; tests stub that method with a
scripted async to avoid hitting the network. Channel-level tests assert
that:
- `outbound_only = True` (so the gateway monitor supervisor skips it),
- `monitor()` raises NotImplementedError,
- `send()` translates SendParams to a chat.postMessage payload,
- error mapping reaches the right SDK error class,
- token resolution prefers credentials store, then SLACK_BOT_TOKEN env.
"""

from __future__ import annotations

import pytest

from sampyclaw.config.credentials import CredentialStore
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.extensions.slack.accounts import SlackAccountRegistry
from sampyclaw.extensions.slack.channel import SLACK_CHANNEL_ID, SlackChannel
from sampyclaw.extensions.slack.client import (
    DEFAULT_BASE_URL,
    SlackApiError,
    SlackWebClient,
)
from sampyclaw.extensions.slack.send import send_message_slack
from sampyclaw.extensions.slack.token import SlackTokenResolver
from sampyclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    MonitorOpts,
    ProbeOpts,
    SendParams,
)
from sampyclaw.plugin_sdk.config_schema import (
    AccountConfig,
    ChannelConfig,
    RootConfig,
)
from sampyclaw.plugin_sdk.error_runtime import (
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)


def _paths(tmp_path) -> SampyclawPaths:  # type: ignore[no-untyped-def]
    p = SampyclawPaths(home=tmp_path)
    p.ensure_home()
    return p


# ─── token resolver ──────────────────────────────────────────────────


def test_token_resolves_from_credentials_store(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    store = CredentialStore(paths)
    store.write("slack", "alerts", {"token": "xoxb-from-store"})
    r = SlackTokenResolver(store)
    assert r.resolve("alerts") == "xoxb-from-store"


def test_token_falls_back_to_env_only_for_main(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    r = SlackTokenResolver(CredentialStore(_paths(tmp_path)))
    assert r.resolve("main") == "xoxb-from-env"
    assert r.resolve("alerts") is None  # env shortcut is only for `main`


def test_token_require_raises_when_missing(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    r = SlackTokenResolver(CredentialStore(_paths(tmp_path)))
    with pytest.raises(UserVisibleError):
        r.require("alerts")


# ─── client _call retry / error mapping ─────────────────────────────


async def test_client_post_message_success_payload(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}

    async def fake_call(self, method, payload):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["payload"] = payload
        return {"ok": True, "ts": "1700000000.000100", "channel": "C0123ABCD"}

    monkeypatch.setattr(SlackWebClient, "_call", fake_call)
    c = SlackWebClient(token="xoxb-x")
    res = await c.post_message(channel="C0123ABCD", text="hello")
    assert captured["method"] == "chat.postMessage"
    assert captured["payload"]["channel"] == "C0123ABCD"
    assert captured["payload"]["text"] == "hello"
    assert res["ts"] == "1700000000.000100"


async def test_client_post_message_requires_text_or_blocks() -> None:
    c = SlackWebClient(token="xoxb-x")
    with pytest.raises(ValueError):
        await c.post_message(channel="C0123ABCD")


async def test_client_raises_slack_api_error_on_ok_false(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_call(self, method, payload):  # type: ignore[no-untyped-def]
        # Real `_call` would convert this into SlackApiError; in this test
        # we hit the path *inside* _call by patching the HTTP layer instead
        # — but here we just assert SlackApiError surfaces correctly.
        raise SlackApiError(
            "channel_not_found", status=200, response={"ok": False, "error": "channel_not_found"}
        )

    monkeypatch.setattr(SlackWebClient, "_call", fake_call)
    c = SlackWebClient(token="xoxb-x")
    with pytest.raises(SlackApiError) as exc_info:
        await c.post_message(channel="CXXX", text="hi")
    assert exc_info.value.error_code == "channel_not_found"


# ─── send.py translation ─────────────────────────────────────────────


async def test_send_message_slack_translates_params(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict = {}

    async def fake_post(self, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return {"ok": True, "ts": "1700000001.000000", "channel": "CALERTS"}

    monkeypatch.setattr(SlackWebClient, "post_message", fake_post)
    c = SlackWebClient(token="xoxb-x")
    params = SendParams(
        target=ChannelTarget(
            channel="slack",
            account_id="alerts",
            chat_id="CALERTS",
            thread_id="1700000000.999",
        ),
        text="cron job ok",
    )
    result = await send_message_slack(c, params)
    assert seen["channel"] == "CALERTS"
    assert seen["text"] == "cron job ok"
    assert seen["thread_ts"] == "1700000000.999"
    assert result.message_id == "1700000001.000000"
    assert result.timestamp == 1700000001.0


async def test_send_message_slack_refuses_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c = SlackWebClient(token="xoxb-x")
    params = SendParams(
        target=ChannelTarget(channel="slack", account_id="a", chat_id="C0"),
    )
    with pytest.raises(UserVisibleError):
        await send_message_slack(c, params)


async def test_send_message_slack_maps_rate_limited(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_post(self, **kwargs):  # type: ignore[no-untyped-def]
        raise SlackApiError("ratelimited", status=429)

    monkeypatch.setattr(SlackWebClient, "post_message", fake_post)
    c = SlackWebClient(token="xoxb-x")
    params = SendParams(
        target=ChannelTarget(channel="slack", account_id="a", chat_id="C0"),
        text="x",
    )
    with pytest.raises(RateLimitedError):
        await send_message_slack(c, params)


async def test_send_message_slack_maps_other_api_error_to_user_visible(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_post(self, **kwargs):  # type: ignore[no-untyped-def]
        raise SlackApiError("channel_not_found", status=200)

    monkeypatch.setattr(SlackWebClient, "post_message", fake_post)
    c = SlackWebClient(token="xoxb-x")
    params = SendParams(
        target=ChannelTarget(channel="slack", account_id="a", chat_id="CDOESNTEXIST"),
        text="x",
    )
    with pytest.raises(UserVisibleError) as ei:
        await send_message_slack(c, params)
    assert "channel_not_found" in str(ei.value)


async def test_send_message_slack_maps_network_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import aiohttp

    async def fake_post(self, **kwargs):  # type: ignore[no-untyped-def]
        raise aiohttp.ClientConnectionError("nope")

    monkeypatch.setattr(SlackWebClient, "post_message", fake_post)
    c = SlackWebClient(token="xoxb-x")
    params = SendParams(
        target=ChannelTarget(channel="slack", account_id="a", chat_id="C0"),
        text="x",
    )
    with pytest.raises(NetworkError):
        await send_message_slack(c, params)


# ─── channel-level invariants ────────────────────────────────────────


async def test_channel_is_outbound_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ch = SlackChannel(token="xoxb-x")
    assert ch.outbound_only is True
    assert ch.id == SLACK_CHANNEL_ID
    with pytest.raises(NotImplementedError):
        await ch.monitor(MonitorOpts(account_id="main", on_inbound=lambda _: None))  # type: ignore[arg-type]


async def test_channel_send_rejects_wrong_target_channel() -> None:
    ch = SlackChannel(token="xoxb-x")
    params = SendParams(
        target=ChannelTarget(channel="telegram", account_id="a", chat_id="42"),
        text="hi",
    )
    with pytest.raises(UserVisibleError):
        await ch.send(params)


async def test_channel_probe_returns_ok_on_auth_test(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_call(self, method, payload):  # type: ignore[no-untyped-def]
        assert method == "auth.test"
        return {"ok": True, "team": "ACME", "user": "alerter", "team_id": "T0", "user_id": "U0"}

    monkeypatch.setattr(SlackWebClient, "_call", fake_call)
    ch = SlackChannel(token="xoxb-x")
    res = await ch.probe(ProbeOpts(account_id="alerts"))
    assert res.ok is True
    assert "alerter" in (res.display_name or "")


async def test_channel_probe_returns_error_on_bad_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_call(self, method, payload):  # type: ignore[no-untyped-def]
        raise SlackApiError("invalid_auth", status=401)

    monkeypatch.setattr(SlackWebClient, "_call", fake_call)
    ch = SlackChannel(token="xoxb-bad")
    res = await ch.probe(ProbeOpts(account_id="alerts"))
    assert res.ok is False
    assert "invalid_auth" in (res.error or "")


# ─── multi-account loader ────────────────────────────────────────────


def test_account_registry_loads_from_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    store = CredentialStore(paths)
    store.write("slack", "alerts", {"token": "xoxb-a"})
    store.write("slack", "ops", {"token": "xoxb-o"})
    cfg = RootConfig(
        channels={
            "slack": ChannelConfig(
                accounts=[
                    AccountConfig(account_id="alerts"),
                    AccountConfig(account_id="ops"),
                ],
            )
        }
    )
    reg = SlackAccountRegistry(paths=paths)
    loaded = reg.load_from_config(cfg)
    assert sorted(loaded) == ["alerts", "ops"]
    assert reg.require("alerts").id == "slack"
    assert reg.require("ops")._client._base_url == DEFAULT_BASE_URL


def test_account_registry_uses_per_account_base_url_override(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    store = CredentialStore(paths)
    store.write("slack", "alerts", {"token": "xoxb-a"})
    cfg = RootConfig(
        channels={
            "slack": ChannelConfig(
                accounts=[
                    # `extra` is allowed (model_config.extra="allow"); base_url
                    # gets stashed in model_extra.
                    AccountConfig.model_validate(
                        {"account_id": "alerts", "base_url": "https://slack-proxy.corp/api"}
                    ),
                ],
            )
        }
    )
    reg = SlackAccountRegistry(paths=paths)
    reg.load_from_config(cfg)
    ch = reg.require("alerts")
    assert ch._client._base_url == "https://slack-proxy.corp/api"


def test_account_registry_skips_account_with_no_token(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    cfg = RootConfig(
        channels={
            "slack": ChannelConfig(
                accounts=[AccountConfig(account_id="alerts")],
            )
        }
    )
    reg = SlackAccountRegistry(paths=paths)
    loaded = reg.load_from_config(cfg)
    assert loaded == []
    assert reg.get("alerts") is None
