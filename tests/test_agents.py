"""Agent registry, echo agent, and dispatcher tests."""

from __future__ import annotations

import pytest

from sampyclaw.agents import (
    AgentRegistry,
    Dispatcher,
    EchoAgent,
    session_key_for,
)
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


def _env(text: str = "hi", sender: str = "user-1") -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id=sender,
        text=text,
        received_at=0.0,
    )


def test_session_key_includes_thread_when_present() -> None:
    t = ChannelTarget(channel="telegram", account_id="main", chat_id="42", thread_id="99")
    assert session_key_for(t) == "telegram:main:42:99"


def test_session_key_without_thread() -> None:
    t = ChannelTarget(channel="telegram", account_id="main", chat_id="42")
    assert session_key_for(t) == "telegram:main:42"


def test_registry_register_and_lookup() -> None:
    r = AgentRegistry()
    agent = EchoAgent()
    r.register(agent)
    assert r.get("echo") is agent
    assert r.require("echo") is agent
    assert r.ids() == ["echo"]


def test_registry_duplicate_raises() -> None:
    r = AgentRegistry()
    r.register(EchoAgent("a"))
    with pytest.raises(ValueError):
        r.register(EchoAgent("a"))


def test_registry_require_missing_raises() -> None:
    r = AgentRegistry()
    with pytest.raises(KeyError):
        r.require("missing")


async def test_echo_agent_yields_prefixed_text() -> None:
    agent = EchoAgent()
    from sampyclaw.agents.base import AgentContext

    ctx = AgentContext(agent_id="echo", session_key="s")
    out = [sp async for sp in agent.handle(_env("hello"), ctx)]
    assert len(out) == 1
    assert out[0].text == "echo: hello"
    assert out[0].target.chat_id == "42"


async def test_echo_agent_empty_text_yields_nothing() -> None:
    agent = EchoAgent()
    from sampyclaw.agents.base import AgentContext

    ctx = AgentContext(agent_id="echo", session_key="s")
    out = [sp async for sp in agent.handle(_env(""), ctx)]
    assert out == []


async def test_dispatcher_routes_to_matching_agent_and_sends() -> None:
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

    sent: list[SendParams] = []

    async def _send(params: SendParams) -> SendResult:
        sent.append(params)
        return SendResult(message_id="m1", timestamp=0.0)

    d = Dispatcher(agents=agents, config=config, send=_send)
    results = await d.dispatch(_env("hello"))
    assert len(results) == 1
    assert len(sent) == 1
    assert sent[0].text == "echo: hello"


async def test_dispatcher_respects_allow_from() -> None:
    agents = AgentRegistry()
    agents.register(EchoAgent())
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=["user-allowed"])},
            )
        }
    )

    async def _send(p: SendParams) -> SendResult:
        return SendResult(message_id="m", timestamp=0.0)

    d = Dispatcher(agents=agents, config=config, send=_send)
    assert await d.dispatch(_env(sender="user-blocked")) == []
    assert len(await d.dispatch(_env(sender="user-allowed"))) == 1


async def test_dispatcher_drops_when_no_matching_agent() -> None:
    agents = AgentRegistry()
    config = RootConfig()

    async def _send(p: SendParams) -> SendResult:
        raise AssertionError("should not send")

    d = Dispatcher(agents=agents, config=config, send=_send)
    assert await d.dispatch(_env()) == []


async def test_dispatcher_drops_when_agent_id_unregistered() -> None:
    agents = AgentRegistry()
    config = RootConfig(
        agents={
            "echo": AgentConfig(
                id="echo",
                channels={"telegram": AgentChannelRouting(allow_from=[])},
            )
        }
    )

    async def _send(p: SendParams) -> SendResult:
        raise AssertionError("should not send")

    d = Dispatcher(agents=agents, config=config, send=_send)
    assert await d.dispatch(_env()) == []


async def test_dispatcher_reuses_session_context_across_turns() -> None:
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

    async def _send(p: SendParams) -> SendResult:
        return SendResult(message_id="m", timestamp=0.0)

    d = Dispatcher(agents=agents, config=config, send=_send)
    await d.dispatch(_env("first"))
    await d.dispatch(_env("second"))
    ctx = d._sessions[("echo", "telegram:main:42")]
    assert len(ctx.history) == 4
