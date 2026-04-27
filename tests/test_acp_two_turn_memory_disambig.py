"""Two-turn scenario: "나는 수원 살아" → "내가 사는 곳 날씨 알려줘".

This test replaces the earlier single-turn fake — that one
pre-populated `MemoryRetriever` and hard-coded
`weather(location="Suwon")` regardless of recall, so a silent
recall regression couldn't fail the assertion. Here both turns are
exercised, and the fake stream's tool-arg decision is **conditioned
on what it actually reads in the user message**:

  - Turn 1: agent receives "나는 수원 살아" with `memory_save`
    registered; the fake stream emits a `memory_save(...)` call. We
    assert memory persisted with Suwon in the chunk text.
  - Turn 2: agent receives "내가 사는 곳 날씨 알려줘". The fake
    stream **inspects the latest user message** (which now carries
    the recall prelude PiAgent prepends), parses out the location,
    and calls `weather(location=<resolved>)`. If recall failed, the
    location it resolves to is the literal deictic phrase, NOT
    "Suwon" — and the assertion `weather_log[0]["location"] ==
    "Suwon"` fires the regression.

Comparison with the prior commit:

  - prior: `weather(location="Suwon")` hardcoded in the stream;
    test would pass even if recall never reached the model.
  - here: stream reads `ctx.messages[-1].content`, finds (or
    fails to find) "Suwon" in the prelude, plugs it in. The test
    actually closes the recall→tool-args loop.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from oxenclaw.acp import manager as manager_mod
from oxenclaw.acp import runtime_registry as registry_mod
from oxenclaw.acp.manager import (
    AcpInitializeSessionInput,
    AcpRunTurnInput,
    get_acp_session_manager,
)
from oxenclaw.acp.pi_agent_runtime import PiAgentAcpRuntime
from oxenclaw.acp.runtime_registry import (
    AcpRuntimeBackend,
    register_acp_runtime_backend,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEvent,
)
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.config import OxenclawPaths
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.memory.tools import (
    memory_get_tool,
    memory_save_tool,
    memory_search_tool,
)
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
from tests._memory_stubs import StubEmbeddings


@pytest.fixture(autouse=True)
def _isolate_globals():
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()
    yield
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


# --- weather tool ---------------------------------------------------------


class _WeatherArgs(BaseModel):
    location: str


def _build_tools(
    *,
    retriever: MemoryRetriever,
    weather_log: list[dict[str, str]],
) -> ToolRegistry:
    """Memory tools + weather. Same shape `oxenclaw acp --backend pi`
    wires up after the F1 fix."""

    async def _weather(args: _WeatherArgs) -> str:
        weather_log.append({"location": args.location})
        return f"Sunny 20°C in {args.location}"

    tools = ToolRegistry()
    tools.register(memory_save_tool(retriever))
    tools.register(memory_search_tool(retriever))
    tools.register(memory_get_tool(retriever))
    tools.register(
        FunctionTool(
            name="weather",
            description="Look up current weather at a city.",
            input_model=_WeatherArgs,
            handler=_weather,
        )
    )
    return tools


async def _drain(it: AsyncIterator[AcpRuntimeEvent]) -> list[AcpRuntimeEvent]:
    out: list[AcpRuntimeEvent] = []
    async for ev in it:
        out.append(ev)
    return out


def _latest_user_text(ctx) -> str:  # type: ignore[no-untyped-def]
    """Pull the most recent user message text from a streaming ctx —
    handles both string content and pi-format multimodal blocks."""
    for msg in reversed(getattr(ctx, "messages", [])):
        role = getattr(msg, "role", None) or (
            msg.get("role") if isinstance(msg, dict) else None
        )
        if role != "user":
            continue
        content = getattr(msg, "content", None) or (
            msg.get("content") if isinstance(msg, dict) else None
        )
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for blk in content:
                text = getattr(blk, "text", None) or (
                    blk.get("text") if isinstance(blk, dict) else None
                )
                if isinstance(text, str):
                    parts.append(text)
            return "\n".join(parts)
        return ""
    return ""


# --- the scenario ---------------------------------------------------------


async def test_two_turn_suwon_weather_resolves_via_memory(
    tmp_path: Path,
) -> None:
    retriever = MemoryRetriever.for_root(_paths(tmp_path), StubEmbeddings())
    weather_log: list[dict[str, str]] = []
    tools = _build_tools(retriever=retriever, weather_log=weather_log)

    # Capture every user message the model sees, across both turns.
    seen_user_messages: list[str] = []
    state = {"calls": 0}

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        latest = _latest_user_text(ctx)
        seen_user_messages.append(latest)

        # ----- TURN 1 -----
        # First model invocation in the session: the user just said
        # "나는 수원 살아". The model recognises this as a personal
        # fact-statement and calls memory_save. Mirrors what the
        # system prompt's memory guide nudges any halfway-capable
        # model to do.
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="ms1", name="memory_save")
            yield ToolUseInputDeltaEvent(
                id="ms1",
                input_delta=(
                    '{"text":"User lives in Suwon, South Korea '
                    '(사용자는 수원에 산다).","tags":["personal-fact","location"]}'
                ),
            )
            yield ToolUseEndEvent(id="ms1")
            yield StopEvent(reason="tool_use")
            return
        if state["calls"] == 2:
            # Post-tool reflection on turn 1.
            yield TextDeltaEvent(delta="기억해 둘게요.")
            yield StopEvent(reason="end_turn")
            return

        # ----- TURN 2 -----
        # By now the user has sent "내가 사는 곳 날씨 알려줘". PiAgent
        # has prepended the recall prelude to that message; this
        # stream READS it and decides the weather tool's `location`
        # argument from what it actually sees. If recall didn't
        # fire, the latest user text is just "내가 사는 곳 날씨
        # 알려줘" with no Suwon, and the conditional below will plug
        # in the literal deictic phrase — failing the test's
        # location==Suwon assertion downstream.
        if state["calls"] == 3:
            resolved_location = _resolve_location_from_recall(latest)
            args_json = (
                '{"location":"' + resolved_location.replace('"', '\\"') + '"}'
            )
            yield ToolUseStartEvent(id="w1", name="weather")
            yield ToolUseInputDeltaEvent(id="w1", input_delta=args_json)
            yield ToolUseEndEvent(id="w1")
            yield StopEvent(reason="tool_use")
            return
        # Final reply on turn 2.
        yield TextDeltaEvent(delta=f"수원은 현재 맑고 20도입니다.")
        yield StopEvent(reason="end_turn")

    register_provider_stream("memory_two_turn_suwon", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="suwon-2turn",
                provider="memory_two_turn_suwon",
                max_output_tokens=512,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="suwon-2turn",
        model_id="suwon-2turn",
        registry=reg,
        auth=InMemoryAuthStorage(  # type: ignore[dict-item]
            {"memory_two_turn_suwon": "sk-test"}
        ),
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        memory=retriever,
        memory_top_k=3,
        memory_inject_into_user=True,
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(
        AcpRuntimeBackend(id="pi", runtime=runtime)
    )
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="2turn",
            agent="suwon-2turn",
            mode="persistent",
            backend_id="pi",
        )
    )
    try:
        # ===== TURN 1 =====
        turn1_events = await _drain(
            mgr.run_turn(
                AcpRunTurnInput(
                    session_key="2turn",
                    text="나는 수원 살아",
                    request_id="r-turn1",
                )
            )
        )

        # Turn 1: the agent must have called memory_save, and memory
        # must now contain a Suwon-bearing chunk.
        ms_cards = [
            e
            for e in turn1_events
            if isinstance(e, AcpEventToolCall) and e.title == "memory_save"
        ]
        assert len(ms_cards) == 2  # pending + completed
        recall_hits_after_turn1 = await retriever.search("내가 사는 곳", k=5)
        assert any(
            "Suwon" in h.chunk.text or "수원" in h.chunk.text
            for h in recall_hits_after_turn1
        ), "memory_save did not actually persist the Suwon fact"

        # ===== TURN 2 =====
        turn2_events = await _drain(
            mgr.run_turn(
                AcpRunTurnInput(
                    session_key="2turn",
                    text="내가 사는 곳 날씨 알려줘",
                    request_id="r-turn2",
                )
            )
        )
    finally:
        await retriever.aclose()

    # ---- the closing-loop assertion ----
    # The fake stream's tool-arg decision was *conditioned on* the
    # recall prelude actually appearing in the user message it saw.
    # If the prelude missed Suwon, weather_log[0]["location"] would
    # be the deictic phrase, NOT "Suwon".
    assert weather_log == [{"location": "Suwon"}], (
        "weather tool received the wrong location — recall did not "
        "drive disambiguation. Latest user message captured by the "
        f"stream on turn 2: {seen_user_messages[-1]!r}"
    )

    # Wire-shape assertion: weather card pair on turn 2.
    weather_cards = [
        e
        for e in turn2_events
        if isinstance(e, AcpEventToolCall) and e.title == "weather"
    ]
    assert len(weather_cards) == 2
    pending, completed = weather_cards
    assert pending.status == "pending"
    assert completed.status == "completed"
    assert pending.tool_call_id == completed.tool_call_id

    # Final assistant text mentions Suwon, in Korean, after the tool.
    text_deltas = [
        e for e in turn2_events if isinstance(e, AcpEventTextDelta)
    ]
    full_text = "".join(t.text for t in text_deltas)
    assert "수원" in full_text

    assert isinstance(turn2_events[-1], AcpEventDone)
    assert turn2_events[-1].stop_reason == "stop"


def _resolve_location_from_recall(user_text: str) -> str:
    """Stand-in for what a real model does with the recall prelude.

    PiAgent prepends `<recalled_memories>` (or a plain bullet list,
    depending on `format_memories_as_prelude`) to the user message
    when memory hits. A real model reads that and resolves "내가
    사는 곳" to the city named there. We approximate that by
    string-matching the prelude for "Suwon" or "수원". If neither
    appears, we plug in the raw deictic phrase — which is exactly
    what a model with no recall would do, and what the test
    expects to FAIL on.
    """
    if "Suwon" in user_text:
        return "Suwon"
    if "수원" in user_text:
        return "Suwon"  # Normalise to English form for the tool.
    # Recall didn't surface the city → return the literal deictic
    # phrase the user typed. The test's location==Suwon assertion
    # will fail meaningfully.
    m = re.search(r"내가\s*사는\s*곳", user_text)
    return m.group(0) if m else user_text.strip()
