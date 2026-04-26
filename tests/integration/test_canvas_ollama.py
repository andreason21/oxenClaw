"""End-to-end live probe: gemma4 → canvas_present → CanvasStore.

Skipped unless `OLLAMA_INTEGRATION=1` and Ollama is reachable. Verifies
the proposal that gemma4:latest can drive the bundled canvas tool with
zero hand-holding: when the user asks to "show" something, the model
emits a canvas_present tool call, the tool writes to the CanvasStore,
and the bus fans out a `present` CanvasEvent.

This is the empirical gate the user requested before merging CV-1.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from sampyclaw.agents import AgentContext, LocalAgent, ToolRegistry, default_tools
from sampyclaw.canvas import (
    CanvasEvent,
    CanvasEventBus,
    CanvasStore,
    reset_default_canvas,
)
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope
from sampyclaw.tools_pkg.canvas import default_canvas_tools


def _envelope(text: str) -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id="canvas-probe",
        text=text,
        received_at=time.time(),
    )


def _build_agent(
    *,
    base_url: str,
    model: str,
    paths: SampyclawPaths,
    store: CanvasStore,
    bus: CanvasEventBus,
) -> LocalAgent:
    tools = ToolRegistry()
    tools.register_all(default_tools())
    tools.register_all(default_canvas_tools(agent_id="canvas-probe", store=store, bus=bus))
    return LocalAgent(
        agent_id="canvas-probe",
        base_url=base_url,
        model=model,
        tools=tools,
        paths=paths,
        system_prompt=(
            "You are sampyClaw with a dashboard canvas. When the user asks to "
            "show, display, render, draw, visualize, or chart something, call "
            "the canvas_present tool. The HTML must be a complete document."
        ),
        warmup=False,
        stream=False,
        max_tool_iterations=2,
    )


async def _drain(agent: LocalAgent, env: InboundEnvelope, ctx: AgentContext) -> None:
    async for _ in agent.handle(env, ctx):
        pass


@pytest.mark.asyncio
async def test_gemma4_present_card_writes_to_store(
    tmp_path,  # type: ignore[no-untyped-def]
    ollama_base_url: str,
    ollama_model: str,
) -> None:
    """The model should emit canvas_present when asked to show a card."""
    reset_default_canvas()
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    store = CanvasStore()
    bus = CanvasEventBus()

    received: list[CanvasEvent] = []

    async def collect() -> None:
        async for evt in bus.stream():
            received.append(evt)

    sub = asyncio.create_task(collect())

    agent = _build_agent(
        base_url=ollama_base_url,
        model=ollama_model,
        paths=paths,
        store=store,
        bus=bus,
    )
    ctx = AgentContext(agent_id="canvas-probe", session_key=uuid.uuid4().hex)

    try:
        await asyncio.wait_for(
            _drain(
                agent,
                _envelope("Show me a centered welcome card that says 'Hello sampyClaw'."),
                ctx,
            ),
            timeout=120.0,
        )
    finally:
        sub.cancel()

    state = store.get("canvas-probe")
    assert state is not None, (
        "gemma4 did not call canvas_present — model is not viable for the canvas skill"
    )
    assert state.html, "canvas_present called but with empty HTML"
    assert state.version >= 1
    assert any(evt.kind == "present" for evt in received), (
        "canvas event bus did not see a 'present' event"
    )
    # Sanity: the HTML the model produced should be substantive.
    assert len(state.html) > 100
