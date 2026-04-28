"""Phase 10: PiAgent end-to-end test via Agent Protocol surface."""

from __future__ import annotations

import asyncio
import json
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
    TextContent,
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
    PiAgent backed by the Ollama catalog default (qwen3.5:9b)."""
    a = build_agent(agent_id="a", provider="pi")
    assert isinstance(a, PiAgent)
    assert a._model.id == "qwen3.5:9b"
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


# ─── Tool-call timing telemetry ──────────────────────────────────────


async def test_pi_agent_records_tool_call_timing(tmp_path: Path) -> None:
    """Regression: after a tool-call turn PiAgent must persist timing metadata
    into ConversationHistory so the dashboard can render tool-call cards."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="tc1", name="slow_tool")
            yield ToolUseInputDeltaEvent(id="tc1", input_delta='{"n":3}')
            yield ToolUseEndEvent(id="tc1")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="done")
            yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_timing", fake_stream)

    class _In(BaseModel):
        n: int

    async def _slow_handler(args: _In) -> str:
        await asyncio.sleep(0.05)
        return f"result-{args.n}"

    tools = ToolRegistry()
    tools.register(
        FunctionTool(
            name="slow_tool", description="a slow tool", input_model=_In, handler=_slow_handler
        )
    )

    reg = _registry_with("test-model", "piagent_timing")
    paths = _paths(tmp_path)
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=paths,
    )
    ctx = AgentContext(agent_id="t", session_key="timing-session")
    outs = [sp async for sp in agent.handle(_inbound("run tool"), ctx)]
    assert outs and outs[0].text == "done"

    session_file = paths.session_file("t", "timing-session")
    assert session_file.exists(), f"expected session file at {session_file}"
    data = json.loads(session_file.read_text())
    msgs = data.get("messages", [])

    # Find the assistant message that carried the tool call.
    tool_call_msgs = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert tool_call_msgs, f"no assistant message with tool_calls found; messages={msgs}"
    tc_msg = tool_call_msgs[0]
    tool_calls = tc_msg["tool_calls"]
    assert len(tool_calls) == 1

    tc = tool_calls[0]
    assert tc["name"] == "slow_tool", tc
    assert tc["status"] == "ok", tc
    assert tc["started_at"] < tc["ended_at"], "started_at must precede ended_at"
    assert tc["ended_at"] - tc["started_at"] >= 0.04, "expected at least ~50ms duration"
    assert tc["output_preview"], "output_preview must be non-empty"
    assert "result-3" in tc["output_preview"], tc["output_preview"]


# ─── debug_assemble + set_model_id (investigation hooks) ────────────


async def test_pi_agent_debug_assemble_returns_structured_payload(tmp_path: Path) -> None:
    """`debug_assemble` is the read-only counterpart to `_system_for` —
    it returns the same prompt the next turn would build, plus per-hit
    memory metadata so the dashboard can show why the model is (or
    isn't) attending to recall."""
    register_provider_stream("piagent_dbgasm", lambda *_: iter([]))
    reg = _registry_with("test-model", "piagent_dbgasm")
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        paths=_paths(tmp_path),
        # Empty memory → debug_assemble still returns a valid payload.
    )
    payload = await agent.debug_assemble("hello world")
    assert payload["model_id"] == "test-model"
    assert payload["agent_id"] == "t"
    assert payload["system_prompt_chars"] == len(payload["system_prompt"])
    assert payload["base_prompt_chars"] > 0
    assert payload["memory_hits"] == []
    assert payload["memory_block"] == ""
    assert payload["memory_weak_threshold"] == 0.30


async def test_pi_agent_recall_prelude_is_above_base_playbook(tmp_path: Path) -> None:
    """Load-bearing claim: the recall prelude must appear ABOVE the
    base playbook in the assembled system prompt. Small local models
    (gemma2/3, qwen2.5:3b) fade on long English playbooks and were
    consistently ignoring `<recalled_memories>` placed at priority 80
    (bottom of prompt). Putting a tight bullet-list prelude at the
    top is the structural fix; this test guards against regressions."""
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.memory.retriever import MemoryRetriever
    from tests._memory_stubs import StubEmbeddings

    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    try:
        await retriever.save("User lives in Suwon, South Korea.")
        register_provider_stream("piagent_prelude", lambda *_: iter([]))
        reg = _registry_with("test-model", "piagent_prelude")
        agent = PiAgent(
            agent_id="t",
            model_id="test-model",
            registry=reg,
            auth=_auth_with_key(reg.list()[0].provider),
            sessions=InMemorySessionManager(),
            paths=paths,
            memory=retriever,
            system_prompt="THE BASE PLAYBOOK STARTS HERE.",
        )
        payload = await agent.debug_assemble("Suwon")
        prompt = payload["system_prompt"]
        idx_prelude = prompt.find("What you already know about this user")
        idx_base = prompt.find("THE BASE PLAYBOOK STARTS HERE.")
        assert idx_prelude >= 0, "prelude must be present"
        assert idx_base >= 0, "base playbook must be present"
        assert idx_prelude < idx_base, (
            "prelude must come BEFORE the base playbook — found "
            f"prelude={idx_prelude} base={idx_base}"
        )
        assert payload["memory_prelude_chars"] > 0
    finally:
        await retriever.aclose()


async def test_pi_agent_debug_assemble_surfaces_recalled_memories(tmp_path: Path) -> None:
    """When a memory retriever returns hits, debug_assemble must include
    them in `memory_hits` AND embed the rendered XML block inside
    `system_prompt`. This is the smoking-gun test for the user's
    'agent doesn't remember' complaint — if this passes but the model
    still ignores recall, the bug is in the model's attention, not in
    our prompt assembly."""
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.memory.retriever import MemoryRetriever
    from tests._memory_stubs import StubEmbeddings

    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    try:
        await retriever.save("User lives in Suwon, South Korea.")
        register_provider_stream("piagent_dbgmem", lambda *_: iter([]))
        reg = _registry_with("test-model", "piagent_dbgmem")
        agent = PiAgent(
            agent_id="t",
            model_id="test-model",
            registry=reg,
            auth=_auth_with_key(reg.list()[0].provider),
            sessions=InMemorySessionManager(),
            paths=paths,
            memory=retriever,
        )
        payload = await agent.debug_assemble("Suwon")
        assert payload["memory_hits"], "expected at least one recall hit"
        assert "Suwon" in payload["memory_block"]
        assert "<recalled_memories>" in payload["system_prompt"]
        assert payload["memory_hits"][0]["score"] >= 0.0
        assert payload["memory_hits"][0]["citation"]
    finally:
        await retriever.aclose()


async def test_pi_agent_set_model_id_swaps_active_model(tmp_path: Path) -> None:
    """`set_model_id` is the runtime A/B hook — flips the underlying
    Model without restarting the gateway. Cache observers must clear
    so a stale provider's cache_control breakpoint isn't sent to a
    different provider."""
    register_provider_stream("piagent_swap", lambda *_: iter([]))
    reg = InMemoryModelRegistry(
        models=[
            Model(id="m1", provider="piagent_swap", max_output_tokens=128, extra={"base_url": "x"}),
            Model(id="m2", provider="piagent_swap", max_output_tokens=128, extra={"base_url": "x"}),
        ]
    )
    agent = PiAgent(
        agent_id="t",
        model_id="m1",
        registry=reg,
        auth=_auth_with_key("piagent_swap"),
        sessions=InMemorySessionManager(),
        paths=_paths(tmp_path),
    )
    # Seed a fake observer to confirm it's cleared.
    agent._observers["fake-session"] = object()  # type: ignore[assignment]
    new_id = agent.set_model_id("m2")
    assert new_id == "m2"
    assert agent._model.id == "m2"
    assert agent._observers == {}


def test_pi_agent_set_model_id_rejects_unknown_model(tmp_path: Path) -> None:
    register_provider_stream("piagent_swapx", lambda *_: iter([]))
    reg = _registry_with("m1", "piagent_swapx")
    agent = PiAgent(
        agent_id="t",
        model_id="m1",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        paths=_paths(tmp_path),
    )
    import pytest

    with pytest.raises(KeyError):
        agent.set_model_id("ghost-model")


def test_default_system_prompt_routes_weather_to_dedicated_tool() -> None:
    """Production hit: gemma4 called `web_search` for "날씨 알려줘"
    instead of the dedicated `weather` tool, then gave up when DDG
    returned 0 hits. The system prompt must now explicitly steer
    weather queries to `weather(city=...)` AND tell the model to
    pull a city from the recalled-memories block when the user
    didn't name one."""
    from oxenclaw.agents.pi_agent import DEFAULT_SYSTEM_PROMPT

    assert "Weather playbook" in DEFAULT_SYSTEM_PROMPT
    assert "`weather` tool" in DEFAULT_SYSTEM_PROMPT
    assert "do NOT use web_search" in DEFAULT_SYSTEM_PROMPT
    assert "weather(city=" in DEFAULT_SYSTEM_PROMPT
    assert "recalled-memories" in DEFAULT_SYSTEM_PROMPT
    # Web research playbook now also has a "specialised tool first" rule.
    assert "weather → `weather`" in DEFAULT_SYSTEM_PROMPT


async def test_pi_agent_injects_recall_prelude_into_user_message(tmp_path: Path) -> None:
    """openclaw vs oxenclaw memory comparison surfaced this gap:
    openclaw doesn't auto-inject — it relies on the model calling
    `memory_search` as a tool. Small local models that don't reliably
    invoke tools used to lose the recall entirely. The fix injects
    the recalled fact list at the TOP of the user message body
    (model-side only — the dashboard still shows what the user
    actually typed). This test guards three things:
      1. The prelude is in the message the model sees
      2. The dashboard history shows the raw user text (no prelude)
      3. The prelude can be turned off via constructor flag
    """
    import json

    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.memory.retriever import MemoryRetriever
    from tests._memory_stubs import StubEmbeddings

    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    seen_user_messages: list[str] = []

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        # Capture what the model actually saw on the user side.
        for msg in ctx.messages:
            if type(msg).__name__ == "UserMessage":
                content = msg.content
                if isinstance(content, str):
                    seen_user_messages.append(content)
                else:
                    for b in content:
                        if isinstance(b, TextContent):
                            seen_user_messages.append(b.text)
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_userinj", fake_stream)
    reg = _registry_with("test-model", "piagent_userinj")
    try:
        await retriever.save("User lives in Suwon, South Korea.")
        agent = PiAgent(
            agent_id="t",
            model_id="test-model",
            registry=reg,
            auth=_auth_with_key(reg.list()[0].provider),
            sessions=InMemorySessionManager(),
            paths=paths,
            memory=retriever,
        )
        ctx = AgentContext(agent_id="t", session_key="user-inj-session")
        async for _ in agent.handle(_inbound("내가 어디 살지?"), ctx):
            pass

        # 1. Model saw the prelude prepended to the user text.
        assert seen_user_messages, "model never saw a user message"
        model_view = seen_user_messages[-1]
        assert "What you already know about this user" in model_view
        assert "Suwon" in model_view
        assert "내가 어디 살지?" in model_view  # raw text still present

        # 2. Dashboard sees ONLY what the user typed.
        session_file = paths.session_file("t", "user-inj-session")
        msgs = json.loads(session_file.read_text())["messages"]
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg["content"] == "내가 어디 살지?"
        assert "Known facts" not in user_msg["content"]
        assert "Suwon" not in user_msg["content"]
    finally:
        await retriever.aclose()


async def test_pi_agent_user_side_recall_can_be_disabled(tmp_path: Path) -> None:
    """`memory_inject_into_user=False` reverts to openclaw-style:
    nothing prepended to the user message; recall lives only in the
    system prompt. Useful for large models where user-side injection
    would just bloat the turn."""
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.memory.retriever import MemoryRetriever
    from tests._memory_stubs import StubEmbeddings

    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    seen: list[str] = []

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        for msg in ctx.messages:
            if type(msg).__name__ == "UserMessage" and isinstance(msg.content, str):
                seen.append(msg.content)
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_userinj_off", fake_stream)
    reg = _registry_with("test-model", "piagent_userinj_off")
    try:
        await retriever.save("User lives in Suwon, South Korea.")
        agent = PiAgent(
            agent_id="t",
            model_id="test-model",
            registry=reg,
            auth=_auth_with_key(reg.list()[0].provider),
            sessions=InMemorySessionManager(),
            paths=paths,
            memory=retriever,
            memory_inject_into_user=False,
        )
        ctx = AgentContext(agent_id="t", session_key="user-inj-off-session")
        async for _ in agent.handle(_inbound("내가 어디 살지?"), ctx):
            pass
        assert seen == ["내가 어디 살지?"], f"expected raw user text only, got {seen!r}"
    finally:
        await retriever.aclose()


async def test_pi_agent_rehydrates_session_from_disk_on_fresh_start(tmp_path: Path) -> None:
    """Production hit: gateway restart → InMemorySessionManager is
    empty → pi-runtime gets `messages=[]` even though the dashboard's
    on-disk history has previous turns. User experience: "agent
    doesn't remember what we just talked about." Fix: rehydrate the
    new pi session from ConversationHistory if a file exists for
    this (agent_id, session_key)."""
    seen: list[int] = []

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        seen.append(len(ctx.messages))
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_rehydrate", fake_stream)
    paths = _paths(tmp_path)
    # Simulate a prior session: dashboard history exists from a
    # previous gateway PID.
    from oxenclaw.agents.history import ConversationHistory

    hist_path = paths.session_file("t", "rehydrate-key")
    hist = ConversationHistory(hist_path)
    hist.append({"role": "user", "content": "내 이름은 윤기야"})
    hist.append({"role": "assistant", "content": "알겠습니다, 윤기님."})
    hist.append({"role": "user", "content": "방금 내가 뭐라고 했지?"})
    hist.append({"role": "assistant", "content": "이름이 윤기라고 하셨어요."})
    hist.save()

    reg = _registry_with("test-model", "piagent_rehydrate")
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),  # fresh, "post-restart"
        paths=paths,
    )
    ctx = AgentContext(agent_id="t", session_key="rehydrate-key")
    async for _ in agent.handle(_inbound("내 이름이 뭐라고 했지?"), ctx):
        pass
    # The pi-runtime should have seen the rehydrated 4 prior messages
    # PLUS the new user turn = 5. Without rehydration, it was 1.
    assert seen[0] >= 5, (
        f"expected pi runtime to see >=5 messages (4 rehydrated + new user turn), got {seen[0]}"
    )


async def test_pi_agent_emits_placeholder_when_model_returns_empty(tmp_path: Path, caplog) -> None:
    """Regression: a model that streams `StopEvent(end_turn)` with no
    text deltas (small local models sometimes do this when a
    restrictive system prompt confuses them) used to result in
    `yielded=0 delivered=0` and a silent dashboard. PiAgent must now:
      - log a WARNING with the final-message structure so operators
        can debug,
      - emit a placeholder SendParams + dashboard history entry so
        the user sees that *something* happened.
    """
    import logging

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        # No text deltas — just an end_turn. Simulates the empty-reply
        # case observed in production with gemma4:latest.
        yield StopEvent(reason="end_turn")

    register_provider_stream("piagent_empty", fake_stream)
    reg = _registry_with("test-model", "piagent_empty")
    paths = _paths(tmp_path)
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        paths=paths,
    )
    ctx = AgentContext(agent_id="t", session_key="empty-session")
    with caplog.at_level(logging.WARNING, logger="oxenclaw.agents.pi"):
        outs = [sp async for sp in agent.handle(_inbound("hello"), ctx)]
    assert outs, "empty-reply path must still yield a placeholder"
    assert "no reply" in outs[0].text.lower()
    warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("no text reply" in m for m in warns), warns
    # Dashboard history captured the placeholder so the user sees it.
    session_file = paths.session_file("t", "empty-session")
    assert session_file.exists()
    msgs = json.loads(session_file.read_text())["messages"]
    asst = next((m for m in msgs if m["role"] == "assistant"), None)
    assert asst is not None
    assert "no reply" in asst["content"].lower()


async def test_pi_agent_assembles_openclaw_ported_sections(tmp_path: Path) -> None:
    """`debug_assemble` must surface the openclaw-ported Execution Bias,
    Skills (mandatory), Memory Recall, and Project Context sections in
    the configured order. Regression guard for the Apr 2026 port from
    `buildAgentSystemPrompt` — see ROADMAP "openclaw system-prompt port"."""
    register_provider_stream("piagent_ported", lambda *_: iter([]))
    reg = _registry_with("test-model", "piagent_ported")

    # Stage AGENTS.md so project_context_dir surfaces a section.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "AGENTS.md").write_text("PROJECT_CONTEXT_BODY")

    # Memory retriever so the recall procedure block fires.
    from oxenclaw.memory.retriever import MemoryRetriever
    from tests._memory_stubs import StubEmbeddings

    paths = _paths(tmp_path)
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    try:
        await retriever.save("User lives in Suwon, South Korea.")
        agent = PiAgent(
            agent_id="t",
            model_id="test-model",
            registry=reg,
            auth=_auth_with_key(reg.list()[0].provider),
            sessions=InMemorySessionManager(),
            paths=paths,
            memory=retriever,
            project_context_dir=project_dir,
        )
        payload = await agent.debug_assemble("Suwon")
        prompt = payload["system_prompt"]

        # All four ported sections must be present.
        assert "## Execution Bias" in prompt
        # `## Skills (mandatory)` only fires when there are skills loaded;
        # bundled skills ship with oxenclaw so this should be non-empty.
        assert "## Skills (mandatory)" in prompt
        assert "## Memory Recall" in prompt
        assert "# Project Context" in prompt
        assert "PROJECT_CONTEXT_BODY" in prompt

        # Procedure-before-data ordering: rules should come before the
        # XML data they describe. Use the closing tag (`</...>`) as the
        # XML-block anchor — the *opening* tag also appears as a text
        # reference inside the base playbook ("the `<available_skills>`
        # block lists installed skills…"), which would false-positive
        # a naive `find("<available_skills>")` lookup.
        assert prompt.find("## Skills (mandatory)") < prompt.find("</available_skills>")
        assert prompt.find("## Memory Recall") < prompt.find("</recalled_memories>")
    finally:
        await retriever.aclose()


async def test_pi_agent_skips_execution_bias_when_disabled(tmp_path: Path) -> None:
    register_provider_stream("piagent_no_exec_bias", lambda *_: iter([]))
    reg = _registry_with("test-model", "piagent_no_exec_bias")
    agent = PiAgent(
        agent_id="t",
        model_id="test-model",
        registry=reg,
        auth=_auth_with_key(reg.list()[0].provider),
        sessions=InMemorySessionManager(),
        paths=_paths(tmp_path),
        include_execution_bias=False,
    )
    payload = await agent.debug_assemble("hello")
    assert "## Execution Bias" not in payload["system_prompt"]


async def test_pi_agent_recall_logs_per_hit_scores_and_warns_when_weak(
    tmp_path: Path, caplog
) -> None:
    """When the top recall score is below `memory_weak_threshold`, the
    agent must (a) still inject the block (so the model has the option)
    and (b) log a WARNING so operators can spot weak recall in
    gateway.log without instrumenting per-turn."""
    import logging

    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.memory.retriever import MemoryRetriever
    from tests._memory_stubs import StubEmbeddings

    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    try:
        await retriever.save("totally unrelated text about astronomy")
        register_provider_stream("piagent_weak", lambda *_: iter([]))
        reg = _registry_with("test-model", "piagent_weak")
        agent = PiAgent(
            agent_id="t",
            model_id="test-model",
            registry=reg,
            auth=_auth_with_key(reg.list()[0].provider),
            sessions=InMemorySessionManager(),
            paths=paths,
            memory=retriever,
            memory_weak_threshold=0.99,  # force "weak" path
        )
        with caplog.at_level(logging.INFO, logger="oxenclaw.agents.pi"):
            payload = await agent.debug_assemble("query that doesn't match astronomy chunk")
        assert payload["memory_block"], "weak recall must still be embedded"
        # Per-hit scores logged in the structured `scores=[...]` field.
        info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
        assert any("scores=[" in m for m in info_msgs)
        warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("below 0.99" in m for m in warns)
    finally:
        await retriever.aclose()
