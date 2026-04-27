"""Phase T3: cron + message tool tests."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.agents.dispatch import Dispatcher
from oxenclaw.agents.echo import EchoAgent
from oxenclaw.agents.registry import AgentRegistry
from oxenclaw.channels.router import ChannelRouter
from oxenclaw.cron.scheduler import CronScheduler
from oxenclaw.cron.store import CronJobStore
from oxenclaw.plugin_sdk.channel_contract import (
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)
from oxenclaw.plugin_sdk.config_schema import RootConfig
from oxenclaw.tools_pkg.cron_tool import cron_tool
from oxenclaw.tools_pkg.message_tool import message_tool

# ─── Fake channel for message tool ──────────────────────────────────


class _FakeChannel:
    id = "dashboard"

    def __init__(self) -> None:
        self.sent: list[SendParams] = []

    async def send(self, params: SendParams) -> SendResult:
        self.sent.append(params)
        return SendResult(message_id=f"m{len(self.sent)}", timestamp=0.0)

    async def probe(self, opts: ProbeOpts) -> ProbeResult:
        return ProbeResult(ok=True, account_id=opts.account_id)

    async def monitor(self, opts):  # type: ignore[no-untyped-def]
        return None


# ─── cron tool ───────────────────────────────────────────────────────


def _make_scheduler(tmp_path: Path) -> CronScheduler:
    config = RootConfig()
    agents = AgentRegistry()
    agents.register(EchoAgent())
    dispatcher = Dispatcher(agents=agents, config=config, send=lambda p: _send_noop(p))
    return CronScheduler(
        store=CronJobStore(path=tmp_path / "cron.json"),
        dispatcher=dispatcher,
    )


async def _send_noop(p: SendParams) -> SendResult:
    return SendResult(message_id="x", timestamp=0.0)


async def test_cron_tool_add_then_list_then_remove(tmp_path: Path) -> None:
    sch = _make_scheduler(tmp_path)
    tool = cron_tool(
        sch,
        default_agent_id="echo",
        default_channel="dashboard",
        default_account_id="main",
        default_chat_id="42",
    )

    # add (uses defaults except schedule + prompt)
    out = await tool.execute({"action": "add", "schedule": "0 9 * * *", "prompt": "morning report"})
    assert "cron added" in out
    assert "0 9 * * *" in out
    job_id = out.split("id=")[1].split()[0]

    # list shows the new job
    out = await tool.execute({"action": "list"})
    assert job_id[:8] in out
    assert "morning report" not in out  # description, not prompt
    # toggle disabled
    out = await tool.execute({"action": "toggle", "job_id": job_id, "enabled": False})
    assert "disabled" in out
    # remove
    out = await tool.execute({"action": "remove", "job_id": job_id})
    assert out == "removed"
    out = await tool.execute({"action": "list"})
    assert out == "no cron jobs"


async def test_cron_tool_rejects_bad_schedule(tmp_path: Path) -> None:
    sch = _make_scheduler(tmp_path)
    tool = cron_tool(
        sch,
        default_agent_id="x",
        default_channel="c",
        default_account_id="a",
        default_chat_id="ch",
    )
    out = await tool.execute({"action": "add", "schedule": "not a cron", "prompt": "p"})
    assert "cron error" in out


async def test_cron_tool_requires_targets_when_no_defaults(tmp_path: Path) -> None:
    sch = _make_scheduler(tmp_path)
    tool = cron_tool(sch)  # no defaults
    out = await tool.execute({"action": "add", "schedule": "* * * * *", "prompt": "p"})
    assert "agent_id/channel/account_id/chat_id required" in out


async def test_cron_tool_remove_unknown(tmp_path: Path) -> None:
    sch = _make_scheduler(tmp_path)
    tool = cron_tool(sch)
    out = await tool.execute({"action": "remove", "job_id": "nope"})
    assert "no job with id" in out


async def test_cron_tool_remove_requires_id(tmp_path: Path) -> None:
    sch = _make_scheduler(tmp_path)
    tool = cron_tool(sch)
    out = await tool.execute({"action": "remove"})
    assert "job_id required" in out


# ─── message tool ────────────────────────────────────────────────────


async def test_message_tool_sends_via_router() -> None:
    router = ChannelRouter()
    fake = _FakeChannel()
    router.register("dashboard", "main", fake)
    tool = message_tool(router)
    out = await tool.execute(
        {"channel": "dashboard", "account_id": "main", "chat_id": "42", "text": "hi from agent"}
    )
    assert "sent message_id=m1" in out
    assert fake.sent and fake.sent[0].text == "hi from agent"


async def test_message_tool_surfaces_no_route_error() -> None:
    router = ChannelRouter()  # nothing registered
    tool = message_tool(router)
    out = await tool.execute(
        {"channel": "dashboard", "account_id": "main", "chat_id": "42", "text": "hi"}
    )
    assert "message error" in out
    assert "no channel plugin" in out


async def test_message_tool_passes_thread_and_reply_meta() -> None:
    router = ChannelRouter()
    fake = _FakeChannel()
    router.register("dashboard", "main", fake)
    tool = message_tool(router)
    await tool.execute(
        {
            "channel": "dashboard",
            "account_id": "main",
            "chat_id": "42",
            "text": "ping",
            "thread_id": "t9",
            "reply_to_message_id": "m1",
        }
    )
    assert fake.sent[0].target.thread_id == "t9"
    assert fake.sent[0].reply_to_message_id == "m1"
