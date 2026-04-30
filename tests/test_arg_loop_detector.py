"""ArgLoopDetector unit tests + run-loop integration."""

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
from oxenclaw.pi.run.arg_loop_detector import ArgLoopDetector, _digest_args
from oxenclaw.pi.streaming import (
    StopEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
)


def test_digest_stable_across_key_order() -> None:
    assert _digest_args({"a": 1, "b": 2}) == _digest_args({"b": 2, "a": 1})


def test_digest_handles_unserialisable() -> None:
    class X:
        pass

    # Should not raise, falls back to str()
    d = _digest_args({"obj": X()})
    assert isinstance(d, str) and len(d) == 16


def test_arg_loop_fires_on_threshold() -> None:
    d = ArgLoopDetector(threshold=3)
    assert not d.observe("t", {"x": 1})
    assert not d.observe("t", {"x": 1})
    assert d.observe("t", {"x": 1})  # 3rd identical call


def test_arg_loop_resets_on_different_args() -> None:
    d = ArgLoopDetector(threshold=3)
    d.observe("t", {"x": 1})
    d.observe("t", {"x": 1})
    d.observe("t", {"x": 2})  # different → reset
    assert d.streak == 1
    d.observe("t", {"x": 2})
    d.observe("t", {"x": 2})
    assert d.streak == 3


def test_arg_loop_resets_on_different_name() -> None:
    d = ArgLoopDetector(threshold=3)
    d.observe("a", {"x": 1})
    d.observe("a", {"x": 1})
    d.observe("b", {"x": 1})  # different name → reset
    assert d.streak == 1


async def test_run_loop_aborts_on_repeated_same_args() -> None:
    """Model calling the SAME registered tool with the SAME args N times
    in a row triggers a structured loop_detection abort."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        tid = f"t{state['calls']}"
        # Real registered tool, but identical args every turn.
        yield ToolUseStartEvent(id=tid, name="echo")
        yield ToolUseInputDeltaEvent(id=tid, input_delta='{"q":"X"}')
        yield ToolUseEndEvent(id=tid)
        yield StopEvent(reason="tool_use")

    register_provider_stream("argloop", fake_stream)
    reg = InMemoryModelRegistry(
        models=[Model(id="m", provider="argloop", max_output_tokens=64, extra={"base_url": "x"})]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"argloop": "x"}))  # type: ignore[dict-item]

    class _Echo(BaseModel):
        q: str

    echo = FunctionTool(
        name="echo",
        description="",
        input_model=_Echo,
        handler=lambda a: f"echo:{a.q}",
    )
    tools = ToolRegistry()
    tools.register(echo)

    cfg = RuntimeConfig(arg_loop_threshold=3, max_tool_iterations=20)
    result = await run_agent_turn(
        model=model,
        api=api,
        system=None,
        history=[],
        tools=list(tools._tools.values()),
        config=cfg,
    )
    assert result.stopped_reason == "loop_detection"
    final_text = result.final_message.content[0].text
    assert "loop-detection abort" in final_text
    assert "echo" in final_text
    # Aborted well before iteration cap.
    assert state["calls"] <= cfg.arg_loop_threshold + 1
