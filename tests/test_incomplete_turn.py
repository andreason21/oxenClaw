"""Incomplete-turn detection + cleanup."""

from __future__ import annotations

from oxenclaw.agents.incomplete_turn import repair_incomplete_turn
from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)


def test_no_op_on_balanced_history() -> None:
    msgs = [
        UserMessage(content="hello"),
        AssistantMessage(content=[TextContent(text="hi")]),
    ]
    counters = repair_incomplete_turn(msgs)
    assert counters == {"trailing_users_dropped": 0, "synthetic_tool_results_added": 0}
    assert len(msgs) == 2


def test_drops_trailing_user_turn() -> None:
    msgs = [
        UserMessage(content="first"),
        AssistantMessage(content=[TextContent(text="reply")]),
        UserMessage(content="orphaned"),
    ]
    counters = repair_incomplete_turn(msgs)
    assert counters["trailing_users_dropped"] == 1
    assert len(msgs) == 2
    assert isinstance(msgs[-1], AssistantMessage)


def test_drops_multiple_trailing_users() -> None:
    msgs = [
        AssistantMessage(content=[TextContent(text="hi")]),
        UserMessage(content="o1"),
        UserMessage(content="o2"),
    ]
    counters = repair_incomplete_turn(msgs)
    assert counters["trailing_users_dropped"] == 2
    assert len(msgs) == 1


def test_balances_orphan_tool_use() -> None:
    msgs = [
        UserMessage(content="run a tool"),
        AssistantMessage(
            content=[
                TextContent(text="calling..."),
                ToolUseBlock(id="t1", name="echo", input={"x": 1}),
            ],
            stop_reason="tool_use",
        ),
        # Crash before tool_result was written.
    ]
    counters = repair_incomplete_turn(msgs)
    assert counters["synthetic_tool_results_added"] == 1
    # Last message should now be a synthetic ToolResultMessage.
    last = msgs[-1]
    assert isinstance(last, ToolResultMessage)
    assert last.results[0].tool_use_id == "t1"
    assert last.results[0].is_error is True


def test_already_paired_tool_use_untouched() -> None:
    msgs = [
        UserMessage(content="run a tool"),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="echo", input={"x": 1})],
            stop_reason="tool_use",
        ),
        ToolResultMessage(results=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        AssistantMessage(content=[TextContent(text="done")]),
    ]
    before = len(msgs)
    counters = repair_incomplete_turn(msgs)
    assert counters["synthetic_tool_results_added"] == 0
    assert counters["trailing_users_dropped"] == 0
    assert len(msgs) == before


def test_combination_orphan_plus_trailing_user() -> None:
    msgs = [
        UserMessage(content="a"),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="x", input={})],
            stop_reason="tool_use",
        ),
        UserMessage(content="b"),  # orphan trailing user
    ]
    counters = repair_incomplete_turn(msgs)
    # Synthetic tool_result inserted, trailing user dropped.
    assert counters["synthetic_tool_results_added"] == 1
    assert counters["trailing_users_dropped"] == 1
    assert isinstance(msgs[-1], ToolResultMessage)
