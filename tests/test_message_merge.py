"""Message merge strategy."""

from __future__ import annotations

from oxenclaw.agents.message_merge import merge_consecutive_same_role
from oxenclaw.pi.messages import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    UserMessage,
)


def test_no_merge_when_alternating() -> None:
    msgs = [
        UserMessage(content="hi"),
        AssistantMessage(content=[TextContent(text="hello")]),
        UserMessage(content="bye"),
    ]
    merged = merge_consecutive_same_role(msgs)
    assert merged == 0
    assert len(msgs) == 3


def test_merges_two_user_strings() -> None:
    msgs = [
        UserMessage(content="part one"),
        UserMessage(content="part two"),
    ]
    merged = merge_consecutive_same_role(msgs)
    assert merged == 1
    assert len(msgs) == 1
    # Plain-string merge stays as plain string.
    assert isinstance(msgs[0].content, str)
    assert "part one" in msgs[0].content
    assert "part two" in msgs[0].content


def test_merges_two_assistant_messages() -> None:
    msgs = [
        AssistantMessage(content=[TextContent(text="alpha")], stop_reason="end_turn"),
        AssistantMessage(content=[TextContent(text="beta")], stop_reason="stop"),
    ]
    merged = merge_consecutive_same_role(msgs)
    assert merged == 1
    assert len(msgs) == 1
    out = msgs[0]
    assert isinstance(out, AssistantMessage)
    texts = [b.text for b in out.content if isinstance(b, TextContent)]
    assert texts == ["alpha", "beta"]
    # Later stop_reason wins.
    assert out.stop_reason == "stop"


def test_user_merge_preserves_images() -> None:
    img = ImageContent(media_type="image/png", data="x")
    msgs = [
        UserMessage(content=[TextContent(text="check this"), img]),
        UserMessage(content="follow-up text"),
    ]
    merge_consecutive_same_role(msgs)
    assert len(msgs) == 1
    blocks = msgs[0].content
    assert any(isinstance(b, ImageContent) for b in blocks)
    assert any(isinstance(b, TextContent) and b.text == "check this" for b in blocks)
    # Separator inserted.
    assert any(isinstance(b, TextContent) and b.text == "---" for b in blocks)


def test_does_not_merge_tool_result_messages() -> None:
    msgs = [
        ToolResultMessage(results=[ToolResultBlock(tool_use_id="t1", content="a")]),
        ToolResultMessage(results=[ToolResultBlock(tool_use_id="t2", content="b")]),
    ]
    merged = merge_consecutive_same_role(msgs)
    assert merged == 0
    assert len(msgs) == 2


def test_three_consecutive_users_collapse_to_one() -> None:
    msgs = [
        UserMessage(content="a"),
        UserMessage(content="b"),
        UserMessage(content="c"),
    ]
    merged = merge_consecutive_same_role(msgs)
    assert merged == 2  # two pairwise merges
    assert len(msgs) == 1
    assert "a" in msgs[0].content
    assert "c" in msgs[0].content


def test_assistant_usage_summed_on_merge() -> None:
    msgs = [
        AssistantMessage(content=[TextContent(text="a")], usage={"input_tokens": 10}),
        AssistantMessage(
            content=[TextContent(text="b")], usage={"input_tokens": 5, "output_tokens": 3}
        ),
    ]
    merge_consecutive_same_role(msgs)
    assert msgs[0].usage == {"input_tokens": 15, "output_tokens": 3}
