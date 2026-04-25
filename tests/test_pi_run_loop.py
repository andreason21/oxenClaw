"""Phase 4: pi run loop — attempt + multi-iteration tool loop."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import sampyclaw.pi.providers  # noqa: F401  registers wrappers
from sampyclaw.pi import (
    Api,
    AssistantMessage,
    Model,
    SimpleStreamOptions,
    TextContent,
    ToolUseBlock,
    UserMessage,
    register_provider_stream,
    text_message,
)
from sampyclaw.pi.run import RuntimeConfig, run_agent_turn, run_attempt
from sampyclaw.pi.streaming import (
    ErrorEvent,
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)


def _model(provider: str = "test") -> Model:
    return Model(id="m", provider=provider, max_output_tokens=512)


def _api() -> Api:
    return Api(base_url="http://test")


# ─── attempt ────────────────────────────────────────────────────────


async def test_attempt_assembles_text_only_message() -> None:
    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="he")
        yield TextDeltaEvent(delta="llo")
        yield UsageEvent(usage={"input_tokens": 5, "output_tokens": 2})
        yield StopEvent(reason="end_turn")

    register_provider_stream("test_text_only", fake)
    result = await run_attempt(
        model=_model("test_text_only"),
        api=_api(),
        system=None,
        messages=[text_message("hi")],
        tools=[],
        config=RuntimeConfig(),
    )
    assert isinstance(result.message, AssistantMessage)
    assert result.message.content[0].text == "hello"  # type: ignore[union-attr]
    assert result.message.stop_reason == "end_turn"
    assert result.usage == {"input_tokens": 5, "output_tokens": 2}


async def test_attempt_assembles_tool_use_with_streamed_args() -> None:
    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield ToolUseStartEvent(id="t1", name="echo")
        yield ToolUseInputDeltaEvent(id="t1", input_delta='{"x":')
        yield ToolUseInputDeltaEvent(id="t1", input_delta="1}")
        yield ToolUseEndEvent(id="t1")
        yield StopEvent(reason="tool_use")

    register_provider_stream("test_tool_use", fake)
    result = await run_attempt(
        model=_model("test_tool_use"),
        api=_api(),
        system=None,
        messages=[text_message("hi")],
        tools=[],
        config=RuntimeConfig(),
    )
    blocks = result.message.content
    tool_uses = [b for b in blocks if isinstance(b, ToolUseBlock)]
    assert tool_uses and tool_uses[0].name == "echo"
    assert tool_uses[0].input == {"x": 1}
    assert result.message.stop_reason == "tool_use"


async def test_attempt_bad_json_args_marked_for_self_correct() -> None:
    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield ToolUseStartEvent(id="t1", name="echo")
        yield ToolUseInputDeltaEvent(id="t1", input_delta="{not json")
        yield ToolUseEndEvent(id="t1")
        yield StopEvent(reason="tool_use")

    register_provider_stream("test_bad_json", fake)
    result = await run_attempt(
        model=_model("test_bad_json"),
        api=_api(),
        system=None,
        messages=[text_message("hi")],
        tools=[],
        config=RuntimeConfig(),
    )
    tu = next(b for b in result.message.content if isinstance(b, ToolUseBlock))
    assert tu.input.get("_parse_error") is True
    assert tu.input.get("_raw") == "{not json"


# ─── full agent turn ────────────────────────────────────────────────


class _Tool:
    def __init__(self, name: str, output: str = "ok") -> None:
        self.name = name
        self.description = f"tool {name}"
        self.input_schema = {"type": "object"}
        self._output = output
        self.calls: list[dict] = []

    async def execute(self, args: dict) -> str:
        self.calls.append(args)
        return self._output


async def test_turn_executes_tool_then_finalizes() -> None:
    """First call → tool_use; second call → end_turn with text."""
    state = {"calls": 0}

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="t1", name="echo")
            yield ToolUseInputDeltaEvent(id="t1", input_delta='{"x":1}')
            yield ToolUseEndEvent(id="t1")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="done")
            yield StopEvent(reason="end_turn")

    register_provider_stream("test_turn", fake)
    tool = _Tool("echo", output="x=1")
    result = await run_agent_turn(
        model=_model("test_turn"),
        api=_api(),
        system=None,
        history=[text_message("go")],
        tools=[tool],
        config=RuntimeConfig(),
    )
    assert result.stopped_reason == "end_turn"
    assert any("done" in b.text for b in result.final_message.content if isinstance(b, TextContent))  # type: ignore[union-attr]
    assert tool.calls == [{"x": 1}]
    # Appended: assistant(tool_use), tool_result, assistant(text)
    assert len(result.appended_messages) == 3


async def test_turn_parallel_tool_calls() -> None:
    started = asyncio.Event()
    started_count = {"n": 0}

    async def slow_tool_exec(args):  # type: ignore[no-untyped-def]
        started_count["n"] += 1
        if started_count["n"] >= 2:
            started.set()
        await started.wait()
        return f"done {args.get('i')}"

    class _Slow(_Tool):
        async def execute(self, args):  # type: ignore[no-untyped-def, override]
            return await slow_tool_exec(args)

    state = {"calls": 0}

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="a", name="slow")
            yield ToolUseInputDeltaEvent(id="a", input_delta='{"i":1}')
            yield ToolUseEndEvent(id="a")
            yield ToolUseStartEvent(id="b", name="slow")
            yield ToolUseInputDeltaEvent(id="b", input_delta='{"i":2}')
            yield ToolUseEndEvent(id="b")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="ok")
            yield StopEvent(reason="end_turn")

    register_provider_stream("test_parallel", fake)
    tool = _Slow("slow")
    result = await asyncio.wait_for(
        run_agent_turn(
            model=_model("test_parallel"),
            api=_api(),
            system=None,
            history=[text_message("go")],
            tools=[tool],
            config=RuntimeConfig(parallel_tools=True),
        ),
        timeout=2.0,
    )
    assert result.stopped_reason == "end_turn"
    assert started_count["n"] == 2


async def test_turn_retries_transient_error() -> None:
    state = {"calls": 0}

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ErrorEvent(message="boom", retryable=True)
            return
        yield TextDeltaEvent(delta="recovered")
        yield StopEvent(reason="end_turn")

    register_provider_stream("test_retry", fake)
    result = await run_agent_turn(
        model=_model("test_retry"),
        api=_api(),
        system=None,
        history=[text_message("go")],
        tools=[],
        config=RuntimeConfig(backoff_initial=0.0, backoff_max=0.0),
    )
    assert result.stopped_reason == "end_turn"
    assert any("recovered" in b.text for b in result.final_message.content if isinstance(b, TextContent))  # type: ignore[union-attr]


async def test_turn_iteration_cap_emits_synthetic_message() -> None:
    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield ToolUseStartEvent(id="t", name="loop")
        yield ToolUseInputDeltaEvent(id="t", input_delta="{}")
        yield ToolUseEndEvent(id="t")
        yield StopEvent(reason="tool_use")

    register_provider_stream("test_loop", fake)
    tool = _Tool("loop", output="ok")
    result = await run_agent_turn(
        model=_model("test_loop"),
        api=_api(),
        system=None,
        history=[text_message("go")],
        tools=[tool],
        config=RuntimeConfig(max_tool_iterations=2),
    )
    assert result.stopped_reason == "iteration_cap"
    assert "max tool iterations" in result.final_message.content[0].text  # type: ignore[union-attr]


async def test_turn_aborts_on_event() -> None:
    abort = asyncio.Event()
    abort.set()

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="should not arrive")
        yield StopEvent(reason="end_turn")

    register_provider_stream("test_abort", fake)
    result = await run_agent_turn(
        model=_model("test_abort"),
        api=_api(),
        system=None,
        history=[text_message("go")],
        tools=[],
        config=RuntimeConfig(abort_event=abort),
    )
    assert result.stopped_reason == "abort"


async def test_turn_unknown_tool_returns_error_result() -> None:
    state = {"calls": 0}

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="t", name="missing")
            yield ToolUseInputDeltaEvent(id="t", input_delta="{}")
            yield ToolUseEndEvent(id="t")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="recovered without tool")
            yield StopEvent(reason="end_turn")

    register_provider_stream("test_missing_tool", fake)
    result = await run_agent_turn(
        model=_model("test_missing_tool"),
        api=_api(),
        system=None,
        history=[text_message("go")],
        tools=[],
        config=RuntimeConfig(),
    )
    assert result.stopped_reason == "end_turn"
    assert result.tool_executions[0].is_error is True
