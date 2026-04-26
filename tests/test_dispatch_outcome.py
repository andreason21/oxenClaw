"""Tests for the dispatcher's single-agent fallback + structured drop reasons."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.dispatch import Dispatcher, DispatchOutcome
from sampyclaw.agents.registry import AgentRegistry
from sampyclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InboundEnvelope,
    SendParams,
    SendResult,
)
from sampyclaw.plugin_sdk.config_schema import (
    AgentChannelRouting,
    AgentConfig,
    RootConfig,
)


class _RecordingAgent:
    """Agent stub that records every envelope and yields a fixed reply."""

    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.received: list[InboundEnvelope] = []

    async def handle(  # type: ignore[no-untyped-def]
        self, inbound: InboundEnvelope, ctx: AgentContext
    ) -> AsyncIterator[SendParams]:
        self.received.append(inbound)
        yield SendParams(target=inbound.target, text=f"reply to {inbound.text}")


def _envelope(*, channel: str = "telegram", sender_id: str = "cli", text: str = "hi"):
    return InboundEnvelope(
        channel=channel,
        account_id="main",
        target=ChannelTarget(channel=channel, account_id="main", chat_id="42"),
        sender_id=sender_id,
        text=text,
        received_at=0.0,
    )


async def _send_ok(p: SendParams) -> SendResult:
    return SendResult(message_id=f"{p.target.chat_id}:msg", timestamp=1.0)


def _registry_with(*agents: _RecordingAgent) -> AgentRegistry:
    reg = AgentRegistry()
    for a in agents:
        reg.register(a)
    return reg


# ─── single-agent fallback ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_agent_used_as_implicit_fallback_when_no_routing():
    """Common case: operator runs `gateway start --provider local` without
    declaring any `agents.*.channels` mapping. The dashboard's chat.send
    should still reach the one registered agent."""
    agent = _RecordingAgent("assistant")
    cfg = RootConfig()  # no agents declared
    dispatcher = Dispatcher(agents=_registry_with(agent), config=cfg, send=_send_ok)

    outcome = await dispatcher.dispatch_with_outcome(_envelope())

    assert isinstance(outcome, DispatchOutcome)
    assert outcome.agent_id == "assistant"
    assert outcome.drop_reason is None
    assert len(outcome.results) == 1
    assert agent.received and agent.received[0].text == "hi"


@pytest.mark.asyncio
async def test_explicit_routing_still_wins_over_single_agent_fallback():
    """When the operator HAS declared per-channel routing, that wins —
    we don't silently re-route to a different agent."""
    main = _RecordingAgent("main")
    other = _RecordingAgent("other")
    cfg = RootConfig(
        agents={
            "main": AgentConfig(
                id="main",
                channels={"telegram": AgentChannelRouting()},
            )
        }
    )
    dispatcher = Dispatcher(agents=_registry_with(main, other), config=cfg, send=_send_ok)

    outcome = await dispatcher.dispatch_with_outcome(_envelope())
    assert outcome.agent_id == "main"
    assert main.received and not other.received


@pytest.mark.asyncio
async def test_multiple_agents_no_routing_drops_with_reason():
    a = _RecordingAgent("a")
    b = _RecordingAgent("b")
    cfg = RootConfig()
    dispatcher = Dispatcher(agents=_registry_with(a, b), config=cfg, send=_send_ok)

    outcome = await dispatcher.dispatch_with_outcome(_envelope())
    assert outcome.agent_id is None
    assert outcome.drop_reason is not None
    assert "telegram" in outcome.drop_reason
    assert not a.received and not b.received


@pytest.mark.asyncio
async def test_no_agents_registered_drops_with_clear_reason():
    cfg = RootConfig()
    dispatcher = Dispatcher(agents=AgentRegistry(), config=cfg, send=_send_ok)

    outcome = await dispatcher.dispatch_with_outcome(_envelope())
    assert outcome.results == []
    assert outcome.drop_reason and "registered" in outcome.drop_reason


# ─── allow_from semantics still hold ─────────────────────────────────


@pytest.mark.asyncio
async def test_allow_from_blocks_sender_and_does_not_fall_through():
    """If routing matches the channel but excludes the sender, we do NOT
    silently fall back to the same agent (or another one) — that would
    defeat the allow_from filter."""
    agent = _RecordingAgent("assistant")
    cfg = RootConfig(
        agents={
            "assistant": AgentConfig(
                id="assistant",
                channels={"telegram": AgentChannelRouting(allow_from=["alice"])},
            )
        }
    )
    dispatcher = Dispatcher(agents=_registry_with(agent), config=cfg, send=_send_ok)

    outcome = await dispatcher.dispatch_with_outcome(_envelope(sender_id="bob"))
    assert outcome.agent_id is None
    assert outcome.drop_reason and "allow_from" in outcome.drop_reason
    assert not agent.received


@pytest.mark.asyncio
async def test_allow_from_permits_listed_sender():
    agent = _RecordingAgent("assistant")
    cfg = RootConfig(
        agents={
            "assistant": AgentConfig(
                id="assistant",
                channels={"telegram": AgentChannelRouting(allow_from=["alice"])},
            )
        }
    )
    dispatcher = Dispatcher(agents=_registry_with(agent), config=cfg, send=_send_ok)

    outcome = await dispatcher.dispatch_with_outcome(_envelope(sender_id="alice"))
    assert outcome.agent_id == "assistant"
    assert outcome.results


# ─── routing references unregistered agent ───────────────────────────


@pytest.mark.asyncio
async def test_routing_to_unregistered_agent_drops_with_diagnostic_reason():
    cfg = RootConfig(
        agents={
            "ghost": AgentConfig(
                id="ghost",
                channels={"telegram": AgentChannelRouting()},
            )
        }
    )
    dispatcher = Dispatcher(agents=AgentRegistry(), config=cfg, send=_send_ok)
    outcome = await dispatcher.dispatch_with_outcome(_envelope())
    assert outcome.agent_id == "ghost"
    assert outcome.drop_reason and "not registered" in outcome.drop_reason


# ─── back-compat: dispatch() still returns list[SendResult] ──────────


@pytest.mark.asyncio
async def test_agent_yielded_but_send_failed_is_not_a_drop():
    """Dashboard scenario: the channel name has no plugin loaded so
    `_send` raises every time. The agent still ran and saved its
    reply to history. We must not classify this as a drop."""
    from sampyclaw.plugin_sdk.error_runtime import UserVisibleError

    agent = _RecordingAgent("assistant")
    cfg = RootConfig()

    async def _failing_send(p: SendParams) -> SendResult:
        raise UserVisibleError(f"no route for {p.target.channel}:{p.target.account_id}")

    dispatcher = Dispatcher(agents=_registry_with(agent), config=cfg, send=_failing_send)
    outcome = await dispatcher.dispatch_with_outcome(_envelope())
    assert outcome.agent_id == "assistant"
    assert outcome.agent_yielded == 1
    assert outcome.results == []
    assert outcome.drop_reason is None
    assert outcome.delivery_warnings
    assert "no route" in outcome.delivery_warnings[0]


@pytest.mark.asyncio
async def test_legacy_dispatch_method_returns_list_of_results():
    agent = _RecordingAgent("assistant")
    cfg = RootConfig()
    dispatcher = Dispatcher(agents=_registry_with(agent), config=cfg, send=_send_ok)
    results = await dispatcher.dispatch(_envelope())
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0].message_id.endswith(":msg")


# ─── chat.send RPC surfaces drop reason in result ────────────────────


@pytest.mark.asyncio
async def test_chat_send_rpc_returns_dropped_status_with_reason(tmp_path: Path):
    """End-to-end: the chat.send handler from cli.gateway_cmd._build_router
    needs to forward the dispatcher's drop reason to the wire result."""
    from sampyclaw.agents.factory import build_agent
    from sampyclaw.approvals import ApprovalManager
    from sampyclaw.channels import ChannelRouter
    from sampyclaw.cli.gateway_cmd import _build_router
    from sampyclaw.config.paths import SampyclawPaths
    from sampyclaw.cron import CronJobStore, CronScheduler

    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()

    agents = AgentRegistry()
    # Two agents with no routing → dispatcher must drop.
    agents.register(build_agent(agent_id="a", provider="echo"))
    agents.register(build_agent(agent_id="b", provider="echo"))
    channel_router = ChannelRouter()
    cfg = RootConfig()
    dispatcher = Dispatcher(agents=agents, config=cfg, send=channel_router.send)
    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=channel_router,
        cron_scheduler=CronScheduler(store=CronJobStore(paths=paths), dispatcher=dispatcher),
        approvals=ApprovalManager(state_path=paths.home / "approvals.json"),
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
    assert resp.error is None
    assert resp.result["status"] == "dropped"
    assert "dashboard" in resp.result["reason"]
