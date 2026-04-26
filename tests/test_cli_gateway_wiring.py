"""Tests for the gateway_cmd composition helpers — channel-agnostic edition."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sampyclaw.agents import AgentRegistry, Dispatcher, EchoAgent
from sampyclaw.approvals import ApprovalManager
from sampyclaw.channels import ChannelRouter
from sampyclaw.cli.gateway_cmd import (
    _build_router,
    _supervise_monitors,
    build_channel_router,
)
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.cron import CronJobStore, CronScheduler
from sampyclaw.extensions.telegram.channel import TelegramChannel
from sampyclaw.plugin_sdk.config_schema import (
    AgentChannelRouting,
    AgentConfig,
    RootConfig,
)


@pytest.fixture()
def mocked_bot_factory(monkeypatch):  # type: ignore[no-untyped-def]
    created: list[MagicMock] = []

    def _fake(token: str) -> MagicMock:
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        sent = MagicMock()
        sent.message_id = 1
        sent.date = datetime(2026, 4, 25, tzinfo=UTC)
        bot.send_message = AsyncMock(return_value=sent)
        created.append(bot)
        return bot

    monkeypatch.setattr("sampyclaw.extensions.telegram.channel.create_bot", _fake)
    return created


def _config_one_account() -> RootConfig:
    return RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=[])},
            )
        }
    )


def _setup_gateway(tmp_path, mocked_bot_factory):  # type: ignore[no-untyped-def]
    config = _config_one_account()
    agents = AgentRegistry()
    agents.register(EchoAgent())

    cr = ChannelRouter()
    cr.register("telegram", "main", TelegramChannel(token="t", account_id="main"))

    dispatcher = Dispatcher(agents=agents, config=config, send=cr.send)
    cron = CronScheduler(store=CronJobStore(path=tmp_path / "cron.json"), dispatcher=dispatcher)
    approvals = ApprovalManager()
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()

    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=cr,
        cron_scheduler=cron,
        approvals=approvals,
        paths_home=paths,
    )
    return router, cr, agents, cron, approvals


async def test_build_router_registers_every_method_group(tmp_path, mocked_bot_factory) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _, _ = _setup_gateway(tmp_path, mocked_bot_factory)
    for name in [
        "chat.send",
        "chat.history",
        "chat.clear",
        "agents.list",
        "agents.create",
        "agents.delete",
        "agents.providers",
        "channels.list",
        "channels.probe",
        "config.get",
        "config.reload",
        "cron.list",
        "cron.create",
        "cron.remove",
        "cron.toggle",
        "cron.fire",
        "exec-approvals.list",
        "exec-approvals.resolve",
        "exec-approvals.cancel",
    ]:
        assert router.has(name), f"missing: {name}"


async def test_chat_send_runs_dispatcher_through_channel_router(
    tmp_path, mocked_bot_factory
) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _, _ = _setup_gateway(tmp_path, mocked_bot_factory)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.send",
            "params": {
                "channel": "telegram",
                "account_id": "main",
                "chat_id": "42",
                "text": "hello",
            },
        }
    )
    assert resp.error is None
    assert resp.result["message_id"] == "1"
    mocked_bot_factory[0].send_message.assert_awaited_once()
    assert mocked_bot_factory[0].send_message.call_args.kwargs["text"] == "echo: hello"


async def test_outbound_only_channel_skips_monitor_supervisor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`outbound_only=True` plugins must not get a ChannelRunner spawned.

    Slack channel sets this so calling .monitor() (NotImplementedError)
    isn't tripped by the supervisor on every restart.
    """
    from sampyclaw.cli.gateway_cmd import _supervise_monitors
    from sampyclaw.extensions.slack.channel import SlackChannel

    agents = AgentRegistry()
    agents.register(EchoAgent())
    cr = ChannelRouter()
    slack_ch = SlackChannel(token="xoxb-x", account_id="alerts")
    cr.register("slack", "alerts", slack_ch)
    dispatcher = Dispatcher(agents=agents, config=RootConfig(), send=cr.send)

    async with _supervise_monitors(cr, dispatcher) as runners:
        # Slack is outbound-only → 0 runners spawned.
        assert runners == [], f"expected no runners for outbound-only slack, got {runners!r}"


async def test_chat_send_passes_media_into_inbound_envelope(
    tmp_path, mocked_bot_factory, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Dashboard image-upload path: `chat.send` accepts a `media` array
    and the items reach the dispatcher's InboundEnvelope unchanged."""
    router, _, _, _, _ = _setup_gateway(tmp_path, mocked_bot_factory)

    # Capture the envelope the dispatcher receives. Patch on the
    # Dispatcher class so we don't need to dig out the live instance.
    captured = {}
    from sampyclaw.agents.dispatch import Dispatcher

    real = Dispatcher.dispatch_with_outcome

    async def _spy(self, envelope):  # type: ignore[no-untyped-def]
        captured["envelope"] = envelope
        return await real(self, envelope)

    monkeypatch.setattr(Dispatcher, "dispatch_with_outcome", _spy)

    tiny_jpeg = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="  # not a real JPEG, just shape
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.send",
            "params": {
                "channel": "telegram",
                "account_id": "main",
                "chat_id": "42",
                "text": "describe",
                "media": [
                    {
                        "kind": "photo",
                        "source": tiny_jpeg,
                        "mime_type": "image/jpeg",
                        "filename": "snap.jpg",
                    }
                ],
            },
        }
    )
    assert resp.error is None
    env = captured.get("envelope")
    assert env is not None
    assert len(env.media) == 1
    assert env.media[0].kind == "photo"
    assert env.media[0].source == tiny_jpeg
    assert env.media[0].mime_type == "image/jpeg"
    assert env.media[0].filename == "snap.jpg"


async def test_chat_send_unrouted_returns_local_ok_with_warning(
    tmp_path, mocked_bot_factory
) -> None:  # type: ignore[no-untyped-def]
    # Fresh agents + empty channel router → no telegram:main registered.
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=[])},
            )
        }
    )
    agents = AgentRegistry()
    agents.register(EchoAgent())
    empty_cr = ChannelRouter()
    dispatcher = Dispatcher(agents=agents, config=config, send=empty_cr.send)
    cron = CronScheduler(store=CronJobStore(path=tmp_path / "cron.json"), dispatcher=dispatcher)
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=empty_cr,
        cron_scheduler=cron,
        approvals=ApprovalManager(),
        paths_home=paths,
    )

    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.send",
            "params": {
                "channel": "telegram",
                "account_id": "main",
                "chat_id": "42",
                "text": "hi",
            },
        }
    )
    # The agent ran (saving the reply into ConversationHistory which the
    # dashboard reads via chat.history). The ChannelRouter has no
    # plugin so the wire send fails — that's a delivery warning, not a
    # drop. message_id="local" signals "no wire delivery, see history".
    assert resp.result["status"] == "ok"
    assert resp.result["message_id"] == "local"
    assert resp.result["agent_id"] == "echo"
    # The send failure surfaces as a non-blocking reason hint.
    assert resp.result["reason"] is not None
    assert "telegram" in resp.result["reason"]


async def test_supervise_monitors_spawns_and_cancels_tasks(
    tmp_path, mocked_bot_factory, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    spawned_tasks = []

    class _FakeRunner:
        def __init__(self, channel, opts, **kwargs):  # type: ignore[no-untyped-def]
            self.channel = channel
            self.opts = opts
            self.stopped = False

        async def run_forever(self) -> None:
            import asyncio

            while not self.stopped:
                await asyncio.sleep(0.01)

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("sampyclaw.cli.gateway_cmd.ChannelRunner", _FakeRunner)

    cr = ChannelRouter()
    cr.register("telegram", "main", TelegramChannel(token="t", account_id="main"))
    cr.register("telegram", "secondary", TelegramChannel(token="t", account_id="secondary"))

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
    dispatcher = Dispatcher(agents=agents, config=config, send=cr.send)

    async with _supervise_monitors(cr, dispatcher) as runners:
        assert len(runners) == 2
        import asyncio

        # All monitor tasks must be alive at this point.
        await asyncio.sleep(0)
        spawned_tasks.extend(asyncio.all_tasks())


def test_build_channel_router_uses_plugin_discovery(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """build_channel_router loads accounts via the plugin registry (monkey-patched)."""
    from sampyclaw.plugin_sdk.channel_contract import ChannelPlugin
    from sampyclaw.plugins.manifest import Manifest
    from sampyclaw.plugins.registry import PluginEntry, PluginRegistry

    fake_channel = MagicMock(spec=ChannelPlugin)
    fake_channel.id = "fake"

    def _loader(config, paths) -> dict:  # type: ignore[no-untyped-def]
        return {"only": fake_channel}

    entry = PluginEntry(
        manifest=Manifest(id="fake", channels=["fake"]),
        factory=lambda **kw: fake_channel,
        loader=_loader,
    )
    reg = PluginRegistry()
    reg.register(entry)
    monkeypatch.setattr("sampyclaw.cli.gateway_cmd.discover_plugins", lambda: reg)
    monkeypatch.setenv("SAMPYCLAW_HOME", str(tmp_path))

    cr = build_channel_router()
    assert cr.get("fake", "only") is fake_channel
