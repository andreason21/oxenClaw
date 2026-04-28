"""History image prune."""

from __future__ import annotations

from oxenclaw.pi.messages import (
    AssistantMessage,
    ImageContent,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.run.history_image_prune import prune_old_images


def _img(label: str = "x") -> ImageContent:
    return ImageContent(media_type="image/png", data=label)


def test_no_op_when_under_keep_limit() -> None:
    msgs = [
        UserMessage(content=[TextContent(text="hi"), _img()]),
        AssistantMessage(content=[TextContent(text="ok")]),
    ]
    pruned = prune_old_images(msgs, keep_recent_user_turns=2)
    assert pruned == 0
    # Image still there.
    blocks = msgs[0].content
    assert any(isinstance(b, ImageContent) for b in blocks)


def test_old_image_replaced_with_placeholder() -> None:
    msgs = [
        UserMessage(content=[TextContent(text="t1"), _img("a")]),
        AssistantMessage(content=[TextContent(text="r1")]),
        UserMessage(content=[TextContent(text="t2"), _img("b")]),
        AssistantMessage(content=[TextContent(text="r2")]),
        UserMessage(content="t3"),
        AssistantMessage(content=[TextContent(text="r3")]),
    ]
    pruned = prune_old_images(msgs, keep_recent_user_turns=2)
    # Image in turn 1 (t1) is older than the most recent 2 user turns
    # (t2, t3) → pruned. Turn 2 (t2) image stays.
    assert pruned == 1
    user1_blocks = msgs[0].content
    assert all(not isinstance(b, ImageContent) for b in user1_blocks)
    assert any(isinstance(b, TextContent) and "pruned" in b.text for b in user1_blocks)
    user2_blocks = msgs[2].content
    assert any(isinstance(b, ImageContent) for b in user2_blocks)


def test_string_user_content_untouched() -> None:
    msgs = [
        UserMessage(content="plain string turn"),
        AssistantMessage(content=[TextContent(text="ok")]),
        UserMessage(content="another"),
        AssistantMessage(content=[TextContent(text="r")]),
        UserMessage(content="latest"),
    ]
    pruned = prune_old_images(msgs, keep_recent_user_turns=1)
    assert pruned == 0  # no images to prune


def test_keep_zero_prunes_everything() -> None:
    msgs = [
        UserMessage(content=[TextContent(text="t1"), _img()]),
        AssistantMessage(content=[TextContent(text="r")]),
        UserMessage(content=[TextContent(text="t2"), _img()]),
    ]
    pruned = prune_old_images(msgs, keep_recent_user_turns=0)
    assert pruned == 2
