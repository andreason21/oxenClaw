"""Loop-detection: abort the turn when the model hammers unknown tools."""

from __future__ import annotations

from pydantic import BaseModel

import oxenclaw.pi.providers  # noqa: F401
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
)


def _registry() -> InMemoryModelRegistry:
    return InMemoryModelRegistry(
        models=[Model(id="m", provider="loopdet", max_output_tokens=64, extra={"base_url": "x"})]
    )


async def test_loop_detection_aborts_after_repeated_unknown_tools() -> None:
    """gemma4 hammered `web_search` after DDG returned 0 hits, looped
    forever. The abort fires after `unknown_tool_threshold` turns of
    all-unknown calls so the user sees a structured error."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        # Every turn emit a tool_use for a NON-EXISTENT tool.
        tid = f"t{state['calls']}"
        yield ToolUseStartEvent(id=tid, name="ghost_tool")
        yield ToolUseInputDeltaEvent(id=tid, input_delta="{}")
        yield ToolUseEndEvent(id=tid)
        yield StopEvent(reason="tool_use")

    register_provider_stream("loopdet", fake_stream)
    reg = _registry()
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"loopdet": "x"}))  # type: ignore[dict-item]
    cfg = RuntimeConfig(unknown_tool_threshold=2, max_tool_iterations=10)
    result = await run_agent_turn(
        model=model, api=api, system=None, history=[], tools=[], config=cfg
    )
    assert result.stopped_reason == "loop_detection"
    final_text = result.final_message.content[0].text
    assert "loop-detection abort" in final_text
    # New behavior (openclaw parity): one tool-list reinjection nudge
    # before structural abort. Worst case: threshold + reinject (1) +
    # threshold again = 2*threshold + 1 calls before the abort fires.
    assert state["calls"] <= 2 * cfg.unknown_tool_threshold + 1


async def test_loop_detection_resets_on_successful_tool() -> None:
    """A real tool call between unknown calls resets the streak —
    the abort only fires for CONSECUTIVE unknown-tool iterations."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        tid = f"t{state['calls']}"
        if state["calls"] in (1, 3):
            yield ToolUseStartEvent(id=tid, name="ghost_tool")
            yield ToolUseInputDeltaEvent(id=tid, input_delta="{}")
            yield ToolUseEndEvent(id=tid)
            yield StopEvent(reason="tool_use")
        elif state["calls"] == 2:
            yield ToolUseStartEvent(id=tid, name="real_tool")
            yield ToolUseInputDeltaEvent(id=tid, input_delta="{}")
            yield ToolUseEndEvent(id=tid)
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="done")
            yield StopEvent(reason="end_turn")

    register_provider_stream("loopdet2", fake_stream)
    reg = InMemoryModelRegistry(
        models=[Model(id="m", provider="loopdet2", max_output_tokens=64, extra={"base_url": "x"})]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"loopdet2": "x"}))  # type: ignore[dict-item]

    class _N(BaseModel):
        pass

    real_tool = FunctionTool(
        name="real_tool",
        description="",
        input_model=_N,
        handler=lambda _a: "ok",
    )
    tools = ToolRegistry()
    tools.register(real_tool)

    cfg = RuntimeConfig(unknown_tool_threshold=2, max_tool_iterations=10)
    result = await run_agent_turn(
        model=model,
        api=api,
        system=None,
        history=[],
        tools=list(tools._tools.values()),
        config=cfg,
    )
    # Should reach end_turn — the real_tool iteration resets the streak.
    assert result.stopped_reason == "end_turn"


async def test_loop_detection_disabled_with_high_threshold() -> None:
    """`unknown_tool_threshold=0` (or very high) effectively disables
    detection — the loop runs to max_tool_iterations as before."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        tid = f"t{state['calls']}"
        yield ToolUseStartEvent(id=tid, name="ghost_tool")
        yield ToolUseInputDeltaEvent(id=tid, input_delta="{}")
        yield ToolUseEndEvent(id=tid)
        yield StopEvent(reason="tool_use")

    register_provider_stream("loopdet3", fake_stream)
    reg = InMemoryModelRegistry(
        models=[Model(id="m", provider="loopdet3", max_output_tokens=64, extra={"base_url": "x"})]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"loopdet3": "x"}))  # type: ignore[dict-item]
    cfg = RuntimeConfig(unknown_tool_threshold=999, max_tool_iterations=3)
    result = await run_agent_turn(
        model=model, api=api, system=None, history=[], tools=[], config=cfg
    )
    assert result.stopped_reason == "iteration_cap"
