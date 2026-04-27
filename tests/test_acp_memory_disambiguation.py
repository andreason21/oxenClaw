"""Scenario: user states a fact ("나는 수원 살아"), then later asks
a question that requires that fact to be resolved ("내가 사는 곳
날씨 알려줘") before a tool can be called.

The whole point is **memory disambiguation drives tool input**:

  - If recall is broken, the model sees "내가 사는 곳" and either
    asks the user to clarify or calls the weather tool with a junk
    location string ("내가 사는 곳" itself).
  - If recall works, the user-side prelude injects "User lives in
    Suwon" into the user message, the model resolves "내가 사는
    곳 → 수원", and the weather tool gets called with
    location="Suwon".

This test pins the second path. Memory is pre-populated. The fake
LLM stream is wired to:

  1. Capture the user message it actually receives — proving the
     recall prelude reached the model.
  2. Call `weather(location="Suwon")` as if the model had read
     the prelude and disambiguated the deictic phrase.
  3. After the tool returns, emit a Korean assistant reply that
     references the location.

Assertions span three layers:

  - **Memory layer**: `MemoryRetriever.search` returns the Suwon
    chunk for the disambiguating query.
  - **Agent layer**: the streaming context's user message contains
    "Suwon" (recall prelude landed); the `weather` tool received
    `location="Suwon"`, not "내가 사는 곳".
  - **ACP wire layer**: the tool_call/tool_call_update pair carries
    title="weather" with consistent tool_call_id, and the final
    assistant text delta mentions Suwon.
"""

from __future__ import annotations

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


# --- shared scaffolding ---------------------------------------------------


class _WeatherArgs(BaseModel):
    location: str


def _build_weather_registry(*, weather_log: list[dict[str, str]]) -> ToolRegistry:
    async def _weather(args: _WeatherArgs) -> str:
        weather_log.append({"location": args.location})
        return f"Sunny 20°C in {args.location}"

    tools = ToolRegistry()
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


# --- step 1: seed the memory with the user's location --------------------


async def test_memory_layer_recalls_suwon_for_deictic_query(
    tmp_path: Path,
) -> None:
    """Sanity check on the memory layer alone: after saving "User
    lives in Suwon", a search for the user's deictic query
    surfaces the Suwon chunk."""
    retriever = MemoryRetriever.for_root(_paths(tmp_path), StubEmbeddings())
    try:
        report = await retriever.save(
            "User lives in Suwon, South Korea (사용자는 수원에 산다).",
            tags=["personal-fact", "location"],
        )
        assert report.added + report.changed >= 1

        # Search for the disambiguating query — the deictic phrase
        # the user used in the second turn.
        hits = await retriever.search("내가 사는 곳", k=5)
        assert hits, "memory recall returned nothing for the deictic query"
        assert any(
            "Suwon" in h.chunk.text or "수원" in h.chunk.text for h in hits
        ), "recall did not surface the Suwon chunk"
    finally:
        await retriever.aclose()


# --- step 2: full scenario through the ACP wire ---------------------------


async def test_suwon_weather_scenario_resolves_deictic_via_memory(
    tmp_path: Path,
) -> None:
    """End-to-end: memory pre-loaded with "user lives in Suwon".
    User asks "내가 사는 곳 날씨 알려줘". The fake LLM stream
    captures what it received (proving recall reached the model)
    and fires `weather(location="Suwon")`. We assert the agent
    ran the tool with the resolved location and that the ACP
    wire shows the matching tool card."""

    # ---- 1. memory pre-populated with the user's location ----
    retriever = MemoryRetriever.for_root(_paths(tmp_path), StubEmbeddings())
    await retriever.save(
        "User lives in Suwon, South Korea (사용자는 수원에 산다).",
        tags=["personal-fact", "location"],
    )

    # ---- 2. weather tool registered ----
    weather_log: list[dict[str, str]] = []
    tools = _build_weather_registry(weather_log=weather_log)

    # ---- 3. fake LLM stream that captures and disambiguates ----
    seen_user_messages: list[str] = []
    state = {"calls": 0}

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        # Capture the *latest* user message the model receives.
        # PiAgent prepends the recall prelude to it.
        latest_user = ""
        for msg in reversed(getattr(ctx, "messages", [])):
            role = getattr(msg, "role", None) or (
                msg.get("role") if isinstance(msg, dict) else None
            )
            if role == "user":
                content = getattr(msg, "content", None) or (
                    msg.get("content") if isinstance(msg, dict) else None
                )
                if isinstance(content, str):
                    latest_user = content
                elif isinstance(content, list):
                    # Pi-format multimodal blocks — stringify text parts.
                    parts: list[str] = []
                    for blk in content:
                        text = getattr(blk, "text", None) or (
                            blk.get("text") if isinstance(blk, dict) else None
                        )
                        if isinstance(text, str):
                            parts.append(text)
                    latest_user = "\n".join(parts)
                break
        seen_user_messages.append(latest_user)

        if state["calls"] == 1:
            # Model "decided" the deictic resolves to Suwon based on
            # the recall prelude it just received in the user message.
            yield TextDeltaEvent(
                delta="확인했습니다. 수원의 현재 날씨를 조회합니다.\n"
            )
            yield ToolUseStartEvent(id="w1", name="weather")
            yield ToolUseInputDeltaEvent(
                id="w1", input_delta='{"location":"Suwon"}'
            )
            yield ToolUseEndEvent(id="w1")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(
                delta="수원은 현재 맑고 20도입니다."
            )
            yield StopEvent(reason="end_turn")

    register_provider_stream("memory_disambig_suwon", fake_stream)

    # ---- 4. wire PiAgent + retriever + tools through the runtime ----
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="suwon-test-model",
                provider="memory_disambig_suwon",
                max_output_tokens=512,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="suwon-acp",
        model_id="suwon-test-model",
        registry=reg,
        auth=InMemoryAuthStorage(  # type: ignore[dict-item]
            {"memory_disambig_suwon": "sk-test"}
        ),
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        memory=retriever,
        memory_top_k=3,
        memory_inject_into_user=True,  # default, but explicit for clarity
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(
        AcpRuntimeBackend(id="pi", runtime=runtime)
    )
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="suwon-1",
            agent="suwon-acp",
            mode="oneshot",
            backend_id="pi",
        )
    )
    try:
        events = await _drain(
            mgr.run_turn(
                AcpRunTurnInput(
                    session_key="suwon-1",
                    text="내가 사는 곳 날씨 알려줘",
                    request_id="r-suwon",
                )
            )
        )
    finally:
        await retriever.aclose()

    # ---- assertions ----

    # Memory layer: recall reached the model. The first model
    # invocation's user message must contain Suwon — that's the
    # PiAgent recall prelude doing its job.
    assert seen_user_messages, "fake stream never ran"
    first_user_message = seen_user_messages[0]
    assert (
        "Suwon" in first_user_message or "수원" in first_user_message
    ), (
        "Recall prelude did not inject the user's location into the user "
        f"message. Got: {first_user_message!r}"
    )

    # Agent layer: weather tool ran with the *resolved* location
    # (Suwon), NOT the deictic phrase.
    assert weather_log == [{"location": "Suwon"}], (
        f"weather tool was called with wrong args: {weather_log!r}"
    )

    # ACP wire layer: exactly one tool_call pair, both naming weather,
    # both sharing one id.
    tool_cards = [e for e in events if isinstance(e, AcpEventToolCall)]
    assert len(tool_cards) == 2
    pending, completed = tool_cards
    assert pending.title == "weather"
    assert completed.title == "weather"
    assert pending.status == "pending"
    assert completed.status == "completed"
    assert pending.tool_call_id == completed.tool_call_id

    # Final assistant text mentions the location, in Korean, post-tool.
    text_deltas = [e for e in events if isinstance(e, AcpEventTextDelta)]
    full_text = "".join(t.text for t in text_deltas)
    assert "수원" in full_text

    # done(stop) at the end.
    assert isinstance(events[-1], AcpEventDone)
    assert events[-1].stop_reason == "stop"


# --- negative control: without memory, the deictic stays unresolved ------


async def test_without_memory_recall_fake_stream_sees_only_deictic(
    tmp_path: Path,
) -> None:
    """Negative control: no memory → no Suwon prelude → the model
    sees only "내가 사는 곳", and (in the path the fake stream
    represents) the test setup itself can detect the recall didn't
    fire. This pins the *contrast* with the positive test, so a
    regression that silently broke recall would land HERE first."""
    weather_log: list[dict[str, str]] = []
    tools = _build_weather_registry(weather_log=weather_log)

    seen_user_messages: list[str] = []

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        latest_user = ""
        for msg in reversed(getattr(ctx, "messages", [])):
            role = getattr(msg, "role", None) or (
                msg.get("role") if isinstance(msg, dict) else None
            )
            if role == "user":
                content = getattr(msg, "content", None) or (
                    msg.get("content") if isinstance(msg, dict) else None
                )
                if isinstance(content, str):
                    latest_user = content
                break
        seen_user_messages.append(latest_user)
        # No tool call — model stays uncertain because it has no
        # location to plug in.
        yield TextDeltaEvent(
            delta="어디에 사는지 알려주시면 날씨를 알려드릴게요."
        )
        yield StopEvent(reason="end_turn")

    register_provider_stream("memory_disambig_no_recall", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="m",
                provider="memory_disambig_no_recall",
                max_output_tokens=256,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="control",
        model_id="m",
        registry=reg,
        auth=InMemoryAuthStorage(  # type: ignore[dict-item]
            {"memory_disambig_no_recall": "sk-test"}
        ),
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        # NO memory= here — that's the whole point.
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(
        AcpRuntimeBackend(id="pi", runtime=runtime)
    )
    mgr = get_acp_session_manager()
    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="ctrl-1",
            agent="control",
            mode="oneshot",
            backend_id="pi",
        )
    )
    events = await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="ctrl-1",
                text="내가 사는 곳 날씨 알려줘",
                request_id="r-ctrl",
            )
        )
    )

    assert seen_user_messages
    first = seen_user_messages[0]
    # Without memory, the deictic phrase is preserved verbatim and
    # NO recall block was prepended.
    assert "내가 사는 곳" in first
    assert "Suwon" not in first
    assert "수원" not in first
    # No weather tool call — there was nothing to resolve to.
    assert weather_log == []
    assert not [e for e in events if isinstance(e, AcpEventToolCall)]
    assert isinstance(events[-1], AcpEventDone)
    assert events[-1].stop_reason == "stop"
