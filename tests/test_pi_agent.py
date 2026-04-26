"""Phase 10: PiAgent end-to-end test via Agent Protocol surface."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

import sampyclaw.pi.providers  # noqa: F401  registers wrappers
from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.factory import SUPPORTED_PROVIDERS, build_agent
from sampyclaw.agents.pi_agent import PiAgent
from sampyclaw.agents.tools import FunctionTool, ToolRegistry
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.pi import (
    InMemoryAuthStorage,
    InMemorySessionManager,
    Model,
    register_provider_stream,
)
from sampyclaw.pi.registry import InMemoryModelRegistry
from sampyclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
)
from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def _paths(tmp_path: Path) -> SampyclawPaths:
    p = SampyclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _inbound(text: str = "hello") -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id="user-1",
        text=text,
        received_at=0.0,
    )


def _registry_with(model_id: str, provider: str) -> InMemoryModelRegistry:
    # Mark as inline by giving the model an explicit base_url so resolve_api
    # doesn't demand a hosted credential. The test stream wrapper ignores it.
    return InMemoryModelRegistry(
        models=[
            Model(
                id=model_id,
                provider=provider,
                max_output_tokens=256,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )


def _auth_with_key(provider: str) -> InMemoryAuthStorage:
    """Auth storage pre-populated for a non-inline test provider."""
    return InMemoryAuthStorage({provider: "sk-test"})  # type: ignore[dict-item]


# ─── Factory wiring ──────────────────────────────────────────────────


def test_pi_provider_registered_in_factory() -> None:
    assert "pi" in SUPPORTED_PROVIDERS
    # `pi` is first → it's the new preferred provider.
    assert SUPPORTED_PROVIDERS[0] == "pi"


def test_factory_builds_pi_agent_with_default_model(tmp_path: Path) -> None:
    a = build_agent(agent_id="a", provider="pi")
    assert isinstance(a, PiAgent)
    assert a._model.id == "gemma4:latest"


# ─── End-to-end turn: streamed text ──────────────────────────────────


async def test_pi_agent_handles_text_only_turn(tmp_path: Path) -> None:
    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="hello ")
        yield TextDeltaEvent(delta="world")
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_text", fake_stream)
    reg = _registry_with("test-model", "piagent_text")
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        paths=_paths(tmp_path),
    )
    ctx = AgentContext(agent_id="t", session_key="s1")
    outs = []
    async for sp in agent.handle(_inbound("hi"), ctx):
        outs.append(sp)
    assert len(outs) == 1
    assert outs[0].text == "hello world"


# ─── End-to-end turn: tool call + recovery ──────────────────────────


async def test_pi_agent_executes_tool_then_finalizes(tmp_path: Path) -> None:
    state = {"calls": 0, "tool_args": None}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="t1", name="echo")
            yield ToolUseInputDeltaEvent(id="t1", input_delta='{"x":7}')
            yield ToolUseEndEvent(id="t1")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="seven")
            yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_tool", fake_stream)

    class _A(BaseModel):
        x: int

    async def _h(args: _A) -> str:
        state["tool_args"] = args.x
        return f"got {args.x}"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="echo", description="d", input_model=_A, handler=_h))

    reg = _registry_with("test-model", "piagent_tool")
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
    )
    ctx = AgentContext(agent_id="t", session_key="s2")
    outs = [sp async for sp in agent.handle(_inbound("compute"), ctx)]
    assert outs and outs[0].text == "seven"
    assert state["tool_args"] == 7


# ─── Session persistence across turns ───────────────────────────────


async def test_pi_agent_persists_transcript_across_turns(tmp_path: Path) -> None:
    seen_messages: list[int] = []

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        seen_messages.append(len(ctx.messages))
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_persist", fake_stream)
    reg = _registry_with("test-model", "piagent_persist")
    sm = InMemorySessionManager()
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=sm,
        paths=_paths(tmp_path),
    )
    ctx = AgentContext(agent_id="t", session_key="cont")
    async for _ in agent.handle(_inbound("first"), ctx):
        pass
    async for _ in agent.handle(_inbound("second"), ctx):
        pass
    # Second turn must see the transcript built by the first.
    assert seen_messages[0] == 1  # only the new user
    assert seen_messages[1] >= 3  # prior user, prior asst, new user
