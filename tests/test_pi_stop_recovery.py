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
from oxenclaw.pi.run.attempt import default_max_tokens_for
from oxenclaw.pi.run.stop_recovery import (
    build_recovery_nudge,
    is_length_truncation,
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


def test_is_length_truncation_only_for_empty_length() -> None:
    msg_empty = AssistantMessage(content=[TextContent(text="")], stop_reason="length")
    assert is_length_truncation(msg_empty)

    # text present → not a length truncation worth recovering
    msg_text = AssistantMessage(content=[TextContent(text="partial")], stop_reason="length")
    assert not is_length_truncation(msg_text)

    # tool_use turn → run-loop handles via the normal tool path
    msg_tool = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="x", input={})],
        stop_reason="length",
    )
    assert not is_length_truncation(msg_tool)

    # plain refusal-class empty (no thinking present) falls through to nudge path.
    msg_refusal = AssistantMessage(content=[TextContent(text="")], stop_reason="end_turn")
    assert not is_length_truncation(msg_refusal)


def test_is_length_truncation_thinking_only_natural_stop() -> None:
    """qwen3.5/deepseek-r1: stop=stop with ThinkingBlock present and no
    visible text means the model thought without responding. Same fix
    as length cutoff (bump max_tokens), so we reuse the path."""
    from oxenclaw.pi.messages import ThinkingBlock

    msg_thinking_stop = AssistantMessage(
        content=[ThinkingBlock(thinking="Let me reason step by step...")],
        stop_reason="stop",
    )
    assert is_length_truncation(msg_thinking_stop)

    msg_thinking_end_turn = AssistantMessage(
        content=[ThinkingBlock(thinking="...")],
        stop_reason="end_turn",
    )
    assert is_length_truncation(msg_thinking_end_turn)

    # Thinking + visible text → NOT a recovery case (model spoke).
    msg_thinking_with_text = AssistantMessage(
        content=[ThinkingBlock(thinking="..."), TextContent(text="Hi.")],
        stop_reason="end_turn",
    )
    assert not is_length_truncation(msg_thinking_with_text)

    # Thinking + tool_use → tool path owns it.
    msg_thinking_with_tool = AssistantMessage(
        content=[
            ThinkingBlock(thinking="..."),
            ToolUseBlock(id="t1", name="x", input={}),
        ],
        stop_reason="tool_use",
    )
    assert not is_length_truncation(msg_thinking_with_tool)


def test_split_thinking_tags_strips_leaked_visible_text() -> None:
    """Some llama.cpp / vLLM builds leak `<think>...</think>` into
    the visible stream. attempt.py post-processor must strip them."""
    from oxenclaw.pi.run.attempt import _split_thinking_tags

    visible, leaked = _split_thinking_tags("Hello <think>internal reasoning here</think> world")
    assert visible == "Hello  world"
    assert "internal reasoning here" in leaked

    # No tags → passthrough.
    visible, leaked = _split_thinking_tags("just text")
    assert visible == "just text"
    assert leaked == ""

    # Only thinking → empty visible (this is the case that flips
    # the assembled message into a length-truncation pattern).
    visible, leaked = _split_thinking_tags("<think>only this</think>")
    assert visible == ""
    assert "only this" in leaked

    # Multi-line thinking with markup-like content inside.
    visible, leaked = _split_thinking_tags(
        "before\n<think>line1\nline2\n{json: 'shaped'}</think>\nafter"
    )
    assert "line1" not in visible
    assert "before" in visible and "after" in visible
    assert "line1" in leaked and "json" in leaked


def test_default_max_tokens_for_thinking_vs_plain() -> None:
    thinking = Model(id="t", provider="ollama", max_output_tokens=8192, supports_thinking=True)
    plain = Model(id="p", provider="ollama", max_output_tokens=8192, supports_thinking=False)
    # thinking → 4× plain default
    assert default_max_tokens_for(thinking) == 4096
    assert default_max_tokens_for(plain) == 1024
    # tiny model is honoured (no upscaling above the model's ceiling)
    tiny = Model(id="x", provider="ollama", max_output_tokens=512, supports_thinking=True)
    assert default_max_tokens_for(tiny) == 512


async def test_run_loop_recovers_from_length_truncation() -> None:
    """Empty `length` reply: run loop should bump max_tokens and retry,
    surfacing the second-call answer instead of returning the empty msg."""
    seen_max_tokens: list[int | None] = []
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        seen_max_tokens.append(ctx.max_tokens)
        state["calls"] += 1
        if state["calls"] == 1:
            yield StopEvent(reason="length")
        else:
            yield TextDeltaEvent(delta="full answer")
            yield StopEvent(reason="end_turn")

    register_provider_stream("lengthrec", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="m",
                provider="lengthrec",  # type: ignore[arg-type]
                max_output_tokens=2048,
                supports_thinking=True,
                extra={"base_url": "x"},
            )
        ]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"lengthrec": "x"}))  # type: ignore[dict-item]
    cfg = RuntimeConfig(
        max_tokens=512,
        length_recovery_attempts=1,
        length_recovery_growth=2.0,
    )
    result = await run_agent_turn(
        model=model, api=api, system=None, history=[], tools=[], config=cfg
    )
    assert state["calls"] == 2
    # First call sees configured 512; second call sees 1024 (2× bump).
    assert seen_max_tokens == [512, 1024]
    assert result.stopped_reason == "end_turn"
    assert "full answer" in result.final_message.content[0].text  # type: ignore[union-attr]


async def test_run_loop_length_recovery_disabled() -> None:
    """When length_recovery_attempts=0, the empty length turn is final."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        yield StopEvent(reason="length")

    register_provider_stream("lengthrec_off", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="m",
                provider="lengthrec_off",  # type: ignore[arg-type]
                max_output_tokens=2048,
                extra={"base_url": "x"},
            )
        ]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"lengthrec_off": "x"}))  # type: ignore[dict-item]
    cfg = RuntimeConfig(length_recovery_attempts=0)
    result = await run_agent_turn(
        model=model, api=api, system=None, history=[], tools=[], config=cfg
    )
    assert state["calls"] == 1
    assert result.stopped_reason == "length"


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
