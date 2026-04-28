"""Preemptive compaction: shrink prompt before sending to provider."""

from __future__ import annotations

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    UserMessage,
)
from oxenclaw.pi.run.preemptive_compaction import (
    CompactionRoute,
    decide,
    estimate_prompt_tokens,
    truncate_tool_results,
)


def test_estimate_token_count_pessimistic_short() -> None:
    """English at 3.5 chars/token: "hello world" → ~3 tokens."""
    n = estimate_prompt_tokens(system="hello world", messages=[])
    assert 3 <= n <= 5


def test_estimate_includes_user_assistant_tool_results() -> None:
    msgs = [
        UserMessage(content="aaaaaaaaaaaa"),  # 12 chars → ~3 tokens
        AssistantMessage(content=[TextContent(text="bbbbbbbbbbbb")]),
        ToolResultMessage(
            results=[
                ToolResultBlock(tool_use_id="t1", content="cccccccccccc"),
            ]
        ),
    ]
    n = estimate_prompt_tokens(system="ddddddd", messages=msgs)
    # Pessimistic estimate at 3.5 chars/token: each ~12-char string
    # contributes ~3 tokens → total ≥ 4×3 minus rounding slack.
    assert n >= 10  # all four contribute


def test_decide_noop_when_under_budget() -> None:
    decision = decide(
        system="short",
        messages=[],
        context_window=8192,
    )
    assert decision.route == CompactionRoute.NOOP
    assert decision.overflow_tokens == 0


def test_decide_truncates_tool_results_when_overflow_fits() -> None:
    """Big tool_result + small budget → route=truncate_tool_results."""
    big_blob = "x" * 20_000
    msgs = [
        ToolResultMessage(
            results=[
                ToolResultBlock(tool_use_id="t1", content=big_blob),
            ]
        ),
    ]
    decision = decide(
        system="",
        messages=msgs,
        context_window=2_000,  # tiny window
        threshold_ratio=0.85,
        reserve_tokens=100,
    )
    assert decision.route == CompactionRoute.TRUNCATE_TOOL_RESULTS
    assert decision.overflow_tokens > 0


def test_decide_compact_then_send_when_no_tool_results_to_trim() -> None:
    big_user = "x" * 50_000
    decision = decide(
        system="",
        messages=[UserMessage(content=big_user)],
        context_window=2_000,
    )
    assert decision.route == CompactionRoute.COMPACT_THEN_SEND


def test_truncate_tool_results_trims_oversized_blobs() -> None:
    msgs = [
        ToolResultMessage(
            results=[
                ToolResultBlock(tool_use_id="t1", content="y" * 5000),
                ToolResultBlock(tool_use_id="t2", content="z" * 100),  # under limit
            ]
        ),
    ]
    removed = truncate_tool_results(msgs, keep_chars=1024)
    # Removed bytes from the first block, second untouched.
    assert removed > 0
    block1, block2 = msgs[0].results
    assert "[...truncated]" in block1.content
    assert block2.content == "z" * 100
