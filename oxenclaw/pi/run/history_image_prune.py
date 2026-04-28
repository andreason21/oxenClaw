"""History image prune — replace old image blocks with placeholders.

Mirrors openclaw `pi-embedded-runner/run/history-image-prune.ts`. In a
long multimodal session, the inline base64 image payloads dominate the
context window. We can safely strip every image older than the most
recent N user turns and replace it with a textual placeholder (the
model already saw it once and either incorporated the info or doesn't
need it again).

Defaults:
  - Keep images from the last 2 user turns.
  - Replace older images with `(image attached earlier — pruned)`.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    ImageContent,
    TextContent,
    UserMessage,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.history_image_prune")


_PLACEHOLDER_TEXT = "(image attached earlier in the conversation — pruned to save context)"


def prune_old_images(
    messages: list[Any],
    *,
    keep_recent_user_turns: int = 2,
) -> int:
    """Walk `messages` in place; replace old `ImageContent` blocks with
    textual placeholders. Returns the count of pruned images.

    Counts user turns from the END backwards; once `keep_recent_user_turns`
    have been seen, every earlier image is fair game. Assistant
    messages don't have ImageContent in our message types, but we
    handle the edge case for resilience.
    """
    if keep_recent_user_turns < 0:
        keep_recent_user_turns = 0
    user_turn_count = 0
    pruned = 0
    # Walk in reverse; mark each user message by index, prune older ones.
    boundary_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, UserMessage):
            user_turn_count += 1
            if user_turn_count > keep_recent_user_turns:
                boundary_idx = i + 1  # everything BEFORE this index is prunable
                break
    if boundary_idx is None:
        boundary_idx = 0  # nothing to keep — prune everything (rare)
    if user_turn_count <= keep_recent_user_turns:
        return 0  # not enough turns to prune anything
    for i in range(boundary_idx):
        msg = messages[i]
        if isinstance(msg, UserMessage):
            new_content, removed = _replace_in_user_content(msg.content)
            if removed:
                msg.content = new_content
                pruned += removed
        elif isinstance(msg, AssistantMessage):
            new_blocks: list[Any] = []
            removed = 0
            for b in msg.content:
                if isinstance(b, ImageContent):
                    new_blocks.append(TextContent(text=_PLACEHOLDER_TEXT))
                    removed += 1
                else:
                    new_blocks.append(b)
            if removed:
                msg.content = new_blocks
                pruned += removed
    if pruned:
        logger.info(
            "history-image-prune: removed %d image block(s) older than "
            "the most recent %d user turn(s)",
            pruned,
            keep_recent_user_turns,
        )
    return pruned


def _replace_in_user_content(
    content: Any,
) -> tuple[Any, int]:
    """Return (new_content, removed_count). Handles both `str` (no
    images) and the typed-block list shape."""
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return content, 0
    new_blocks: list[Any] = []
    removed = 0
    for b in content:
        if isinstance(b, ImageContent):
            new_blocks.append(TextContent(text=_PLACEHOLDER_TEXT))
            removed += 1
        else:
            new_blocks.append(b)
    return new_blocks, removed


__all__ = ["prune_old_images"]
