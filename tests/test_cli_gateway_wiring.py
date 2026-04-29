"""Tests for the gateway_cmd composition helpers — channel-agnostic edition."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from oxenclaw.agents import AgentRegistry, Dispatcher, EchoAgent
from oxenclaw.approvals import ApprovalManager
from oxenclaw.channels import ChannelRouter
from oxenclaw.cli.gateway_cmd import (
    _build_router,
    _supervise_monitors,
    build_channel_router,
)
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.cron import CronJobStore, CronScheduler
from oxenclaw.plugin_sdk.channel_contract import (
    MonitorOpts,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)
from oxenclaw.plugin_sdk.config_schema import (
    AgentChannelRouting,
    AgentConfig,
    RootConfig,
)


class _StubChannel:
    """Minimal ChannelPlugin used to drive monitor + send wiring in tests."""

    def __init__(self, *, channel_id: str = "stub", account_id: str = "main") -> None:
        self.id = channel_id
        self.account_id = account_id
        self.sent: list[SendParams] = []

    async def send(self, params: SendParams) -> SendResult:
        self.sent.append(params)
        return SendResult(message_id="1", timestamp=0.0)

    async def monitor(self, opts: MonitorOpts) -> None:
        # Idle forever — the supervisor cancels via task.cancel().
        await asyncio.Event().wait()

    async def probe(self, opts: ProbeOpts) -> ProbeResult:
        return ProbeResult(ok=True, account_id=opts.account_id)

    async def aclose(self) -> None:
        return None


def _config_one_account(channel_id: str = "stub") -> RootConfig:
    return RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={channel_id: AgentChannelRouting(allow_from=[])},
            )
        }
    )


def _setup_gateway(tmp_path, *, with_sessions: bool = False):  # type: ignore[no-untyped-def]
    config = _config_one_account()
    agents = AgentRegistry()
    agents.register(EchoAgent())

    cr = ChannelRouter()
    stub = _StubChannel(channel_id="stub", account_id="main")
    cr.register("stub", "main", stub)

    dispatcher = Dispatcher(agents=agents, config=config, send=cr.send)
    cron = CronScheduler(store=CronJobStore(path=tmp_path / "cron.json"), dispatcher=dispatcher)
    approvals = ApprovalManager()
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()

    extra: dict = {}
    if with_sessions:
        from oxenclaw.pi.lifecycle import LifecycleBus
        from oxenclaw.pi.persistence import SQLiteSessionManager

        extra["session_manager"] = SQLiteSessionManager(paths.home / "sessions.db")
        extra["lifecycle_bus"] = LifecycleBus()

    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=cr,
        cron_scheduler=cron,
        approvals=approvals,
        paths_home=paths,
        **extra,
    )
    return router, cr, agents, cron, approvals, stub


async def test_build_router_registers_every_method_group(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, *_ = _setup_gateway(tmp_path)
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
    # sessions.* is opt-in: omitted when no SessionManager is supplied.
    assert not router.has("sessions.list")


async def test_build_router_registers_sessions_when_manager_supplied(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, *_ = _setup_gateway(tmp_path, with_sessions=True)
    for name in [
        "sessions.list",
        "sessions.get",
        "sessions.preview",
        "sessions.reset",
        "sessions.fork",
        "sessions.archive",
        "sessions.delete",
        "sessions.compact",
    ]:
        assert router.has(name), f"missing: {name}"


async def test_chat_send_runs_dispatcher_through_channel_router(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _cr, _agents, _cron, _appr, stub = _setup_gateway(tmp_path)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.send",
            "params": {
                "channel": "stub",
                "account_id": "main",
                "chat_id": "42",
                "text": "hello",
            },
        }
    )
    assert resp.error is None
    assert resp.result["message_id"] == "1"
    assert len(stub.sent) == 1
    assert stub.sent[0].text == "echo: hello"


async def test_outbound_only_channel_skips_monitor_supervisor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`outbound_only=True` plugins must not get a ChannelRunner spawned.

    Slack channel sets this so calling .monitor() (NotImplementedError)
    isn't tripped by the supervisor on every restart.
    """
    from oxenclaw.extensions.slack.channel import SlackChannel

    agents = AgentRegistry()
    agents.register(EchoAgent())
    cr = ChannelRouter()
    fake_client = MagicMock()
    fake_client._call = AsyncMock(return_value={"ok": True})
    fake_client.aclose = AsyncMock()
    slack_ch = SlackChannel(token="xoxb-x", account_id="alerts", client=fake_client)
    cr.register("slack", "alerts", slack_ch)
    dispatcher = Dispatcher(agents=agents, config=RootConfig(), send=cr.send)

    async with _supervise_monitors(cr, dispatcher) as runners:
        # Slack is outbound-only → 0 runners spawned.
        assert runners == [], f"expected no runners for outbound-only slack, got {runners!r}"


async def test_chat_send_passes_media_into_inbound_envelope(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Dashboard image-upload path: `chat.send` accepts a `media` array
    and the items reach the dispatcher's InboundEnvelope unchanged."""
    router, *_ = _setup_gateway(tmp_path)

    captured = {}
    from oxenclaw.agents.dispatch import Dispatcher as _Dispatcher

    real = _Dispatcher.dispatch_with_outcome

    async def _spy(self, envelope):  # type: ignore[no-untyped-def]
        captured["envelope"] = envelope
        return await real(self, envelope)

    monkeypatch.setattr(_Dispatcher, "dispatch_with_outcome", _spy)

    tiny_jpeg = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.send",
            "params": {
                "channel": "stub",
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


async def test_chat_send_unrouted_returns_local_ok_with_warning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Fresh agents + empty channel router → no dashboard:main registered.
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"dashboard": AgentChannelRouting(allow_from=[])},
            )
        }
    )
    agents = AgentRegistry()
    agents.register(EchoAgent())
    empty_cr = ChannelRouter()
    dispatcher = Dispatcher(agents=agents, config=config, send=empty_cr.send)
    cron = CronScheduler(store=CronJobStore(path=tmp_path / "cron.json"), dispatcher=dispatcher)
    paths = OxenclawPaths(home=tmp_path)
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
                "channel": "dashboard",
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
    assert resp.result["reason"] is not None
    assert "dashboard" in resp.result["reason"]


async def test_supervise_monitors_spawns_and_cancels_tasks(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _FakeRunner:
        def __init__(self, channel, opts, **kwargs):  # type: ignore[no-untyped-def]
            self.channel = channel
            self.opts = opts
            self.stopped = False

        async def run_forever(self) -> None:
            while not self.stopped:
                await asyncio.sleep(0.01)

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("oxenclaw.cli.gateway_cmd.ChannelRunner", _FakeRunner)

    cr = ChannelRouter()
    cr.register("stub", "main", _StubChannel(channel_id="stub", account_id="main"))
    cr.register("stub", "secondary", _StubChannel(channel_id="stub", account_id="secondary"))

    agents = AgentRegistry()
    agents.register(EchoAgent())
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"stub": AgentChannelRouting(allow_from=[])},
            )
        }
    )
    dispatcher = Dispatcher(agents=agents, config=config, send=cr.send)

    async with _supervise_monitors(cr, dispatcher) as runners:
        assert len(runners) == 2
        await asyncio.sleep(0)


def test_build_channel_router_uses_plugin_discovery(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """build_channel_router loads accounts via the plugin registry (monkey-patched)."""
    from oxenclaw.plugin_sdk.channel_contract import ChannelPlugin
    from oxenclaw.plugins.manifest import Manifest
    from oxenclaw.plugins.registry import PluginEntry, PluginRegistry

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
    monkeypatch.setattr("oxenclaw.cli.gateway_cmd.discover_plugins", lambda: reg)
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))

    cr = build_channel_router()
    assert cr.get("fake", "only") is fake_channel
