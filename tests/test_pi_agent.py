"""Phase 10: PiAgent end-to-end test via Agent Protocol surface."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

import oxenclaw.pi.providers  # noqa: F401  registers wrappers
from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.factory import SUPPORTED_PROVIDERS, build_agent
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.pi import (
    InMemoryAuthStorage,
    InMemorySessionManager,
    Model,
    register_provider_stream,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
)
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _inbound(text: str = "hello") -> InboundEnvelope:
    return InboundEnvelope(
        channel="dashboard",
        account_id="main",
        target=ChannelTarget(channel="dashboard", account_id="main", chat_id="42"),
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


def test_pi_runtime_drives_every_catalog_provider() -> None:
    """Post-rc.15: there's no `pi` provider — pi is the *runtime*, and
    every catalog provider id (ollama / anthropic / openai / google /
    vllm / etc.) routes through it. The legacy `pi` name remains
    accepted via the alias map so pre-rc.15 configs keep working."""
    from oxenclaw.agents.factory import CATALOG_PROVIDERS, LEGACY_ALIASES

    assert "ollama" in CATALOG_PROVIDERS
    assert "anthropic" in CATALOG_PROVIDERS
    assert "pi" in LEGACY_ALIASES
    assert LEGACY_ALIASES["pi"] == "ollama"
    assert set(SUPPORTED_PROVIDERS) >= set(CATALOG_PROVIDERS)


def test_factory_builds_pi_agent_for_legacy_pi_alias(tmp_path: Path) -> None:
    """`provider='pi'` is a legacy alias; the factory still produces a
    PiAgent backed by the Ollama catalog default (gemma4:latest)."""
    a = build_agent(agent_id="a", provider="pi")
    assert isinstance(a, PiAgent)
    assert a._model.id == "gemma4:latest"
    assert a._model.provider == "ollama"


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


# ─── Dashboard-format ConversationHistory persistence ────────────────


async def test_pi_agent_writes_dashboard_conversation_history(tmp_path: Path) -> None:
    """Regression: PiAgent must populate ConversationHistory so the
    dashboard's `chat.history` poll surfaces the turn. Pre-fix the pi
    runtime persisted the rich transcript only via SessionManager and
    the dashboard saw an empty conversation."""
    import json

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="answer text")
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_dashlog", fake_stream)
    reg = _registry_with("test-model", "piagent_dashlog")
    paths = _paths(tmp_path)
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        paths=paths,
    )
    ctx = AgentContext(agent_id="t", session_key="dashboard:main:42")
    async for _ in agent.handle(_inbound("question"), ctx):
        pass

    session_file = paths.session_file("t", "dashboard:main:42")
    assert session_file.exists(), f"expected dashboard history at {session_file}"
    data = json.loads(session_file.read_text())
    msgs = data.get("messages", [])
    roles = [m["role"] for m in msgs]
    assert "user" in roles and "assistant" in roles, roles
    # The user turn must carry the inbound text and the assistant turn the reply.
    user_msg = next(m for m in msgs if m["role"] == "user")
    asst_msg = next(m for m in msgs if m["role"] == "assistant")
    assert user_msg["content"] == "question"
    assert asst_msg["content"] == "answer text"
