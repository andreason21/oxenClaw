"""Replay-time tool-call sanitiser."""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolUseBlock,
    UserMessage,
)
from oxenclaw.pi.run.replay_sanitizer import sanitize_replay_tool_calls


def _well_formed_block(name: str = "get_time", id_: str = "t1") -> ToolUseBlock:
    return ToolUseBlock(id=id_, name=name, input={})


def test_well_formed_blocks_pass_through_unchanged() -> None:
    msgs: list[Any] = [
        UserMessage(content="hi"),
        AssistantMessage(
            content=[TextContent(text="ok"), _well_formed_block()],
            stop_reason="tool_use",
        ),
    ]
    snapshot = [type(m) for m in msgs]
    report = sanitize_replay_tool_calls(msgs, allowed_tool_names={"get_time"})
    assert report.dropped_blocks == 0
    assert report.dropped_assistant_messages == 0
    assert [type(m) for m in msgs] == snapshot


def test_drops_block_with_empty_id() -> None:
    bad = ToolUseBlock(id="", name="get_time", input={})
    msgs: list[Any] = [
        AssistantMessage(content=[TextContent(text="x"), bad], stop_reason="tool_use"),
    ]
    report = sanitize_replay_tool_calls(msgs, allowed_tool_names={"get_time"})
    assert report.dropped_blocks == 1
    assert report.dropped_assistant_messages == 0
    # The TextContent survives.
    assert len(msgs) == 1
    assert isinstance(msgs[0], AssistantMessage)
    assert all(not isinstance(b, ToolUseBlock) for b in msgs[0].content)


def test_drops_block_with_unregistered_name() -> None:
    bad = ToolUseBlock(id="t1", name="unknown_tool", input={})
    msgs: list[Any] = [
        AssistantMessage(content=[bad], stop_reason="tool_use"),
    ]
    report = sanitize_replay_tool_calls(msgs, allowed_tool_names={"get_time"})
    assert report.dropped_blocks == 1
    # Whole assistant message dropped — empty after the bad block was removed.
    assert report.dropped_assistant_messages == 1
    assert msgs == []


def test_drops_block_with_whitespace_in_name() -> None:
    bad = ToolUseBlock(id="t1", name="get time", input={})
    msgs: list[Any] = [AssistantMessage(content=[bad], stop_reason="tool_use")]
    report = sanitize_replay_tool_calls(msgs)  # no allowlist
    assert report.dropped_blocks == 1


def test_drops_block_with_overlong_name() -> None:
    bad = ToolUseBlock(id="t1", name="x" * 200, input={})
    msgs: list[Any] = [AssistantMessage(content=[bad], stop_reason="tool_use")]
    report = sanitize_replay_tool_calls(msgs)
    assert report.dropped_blocks == 1


def test_no_allowlist_accepts_any_well_formed_name() -> None:
    block = ToolUseBlock(id="t1", name="custom_xyz_42", input={"a": 1})
    msgs: list[Any] = [AssistantMessage(content=[block], stop_reason="tool_use")]
    report = sanitize_replay_tool_calls(msgs)  # allowed=None
    assert report.dropped_blocks == 0
    assert msgs[0].content[0] is block


def test_keeps_partial_assistant_when_some_blocks_survive() -> None:
    good = _well_formed_block(name="get_time", id_="g1")
    bad = ToolUseBlock(id="", name="get_time", input={})  # bad id
    msgs: list[Any] = [
        AssistantMessage(
            content=[TextContent(text="thinking"), good, bad],
            stop_reason="tool_use",
        ),
    ]
    report = sanitize_replay_tool_calls(msgs, allowed_tool_names={"get_time"})
    assert report.dropped_blocks == 1
    assert report.dropped_assistant_messages == 0
    assert len(msgs) == 1
    surviving = msgs[0].content
    assert any(isinstance(b, TextContent) for b in surviving)
    assert any(isinstance(b, ToolUseBlock) and b.id == "g1" for b in surviving)


def test_idempotent() -> None:
    msgs: list[Any] = [
        AssistantMessage(content=[_well_formed_block()], stop_reason="tool_use"),
    ]
    snapshot = list(msgs)
    r1 = sanitize_replay_tool_calls(msgs, allowed_tool_names={"get_time"})
    r2 = sanitize_replay_tool_calls(msgs, allowed_tool_names={"get_time"})
    assert r1.dropped_blocks == 0 and r2.dropped_blocks == 0
    assert msgs == snapshot
