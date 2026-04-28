"""Stop-reason recovery: re-ask once when the model returns nothing."""

from __future__ import annotations

import oxenclaw.pi.providers  # noqa: F401
from oxenclaw.pi import (
    AssistantMessage,
    InMemoryAuthStorage,
    Model,
    TextContent,
    ToolUseBlock,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.run.stop_recovery import (
    build_recovery_nudge,
    is_recoverable_empty,
)
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent


def test_is_recoverable_empty_for_blank_end_turn() -> None:
    msg = AssistantMessage(content=[TextContent(text="")], stop_reason="end_turn")
    assert is_recoverable_empty(msg)


def test_is_recoverable_empty_for_refusal() -> None:
    msg = AssistantMessage(content=[TextContent(text="")], stop_reason="refusal")
    assert is_recoverable_empty(msg)


def test_is_recoverable_empty_skips_text_replies() -> None:
    msg = AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")
    assert not is_recoverable_empty(msg)


def test_is_recoverable_empty_skips_tool_use_turns() -> None:
    msg = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="x", input={})],
        stop_reason="tool_use",
    )
    assert not is_recoverable_empty(msg)


def test_recovery_nudge_for_refusal_mentions_filter() -> None:
    nudge = build_recovery_nudge("refusal")
    assert "refusal" in nudge.content
    assert "retry" in nudge.content


def test_recovery_nudge_for_empty_mentions_tools() -> None:
    nudge = build_recovery_nudge("end_turn")
    assert "empty" in nudge.content
    assert "memory_search" in nudge.content


async def test_run_loop_re_asks_on_empty_then_succeeds() -> None:
    """Empty-then-text: the run loop should NOT terminate after the
    first empty turn — it appends a nudge user message and re-asks."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            # Empty reply on first call.
            yield StopEvent(reason="end_turn")
        else:
            yield TextDeltaEvent(delta="real answer")
            yield StopEvent(reason="end_turn")

    register_provider_stream("stoprec", fake_stream)
    reg = InMemoryModelRegistry(
        models=[Model(id="m", provider="stoprec", max_output_tokens=64, extra={"base_url": "x"})]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"stoprec": "x"}))  # type: ignore[dict-item]
    cfg = RuntimeConfig(stop_reason_recovery_attempts=1)
    result = await run_agent_turn(
        model=model,
        api=api,
        system=None,
        history=[],
        tools=[],
        config=cfg,
    )
    assert state["calls"] == 2  # first empty, second real
    assert result.stopped_reason == "end_turn"
    assert "real answer" in result.final_message.content[0].text


async def test_run_loop_does_not_re_ask_when_disabled() -> None:
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        yield StopEvent(reason="end_turn")

    register_provider_stream("stoprec_off", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(id="m", provider="stoprec_off", max_output_tokens=64, extra={"base_url": "x"})
        ]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"stoprec_off": "x"}))  # type: ignore[dict-item]
    cfg = RuntimeConfig(stop_reason_recovery_attempts=0)
    result = await run_agent_turn(
        model=model,
        api=api,
        system=None,
        history=[],
        tools=[],
        config=cfg,
    )
    assert state["calls"] == 1
    assert result.stopped_reason == "end_turn"
