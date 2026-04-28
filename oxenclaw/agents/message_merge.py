"""Message merge strategy — coalesce consecutive same-role messages.

Mirrors openclaw `pi-embedded-runner/run/message-merge-strategy.ts`.
Some providers (Anthropic, Google, etc.) reject prompts where two
consecutive messages share the same role. Sources of duplicates:

  - Rapid user typing (multiple chat.send within a debounce window).
  - Tool-result followed by an assistant text turn that the model
    streams in two separate AssistantMessages.
  - Recovery nudges that produce a synthetic UserMessage adjacent to
    the user's actual one.

This module merges consecutive same-role pairs into one message,
preserving content order and types.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.message_merge")


def merge_consecutive_same_role(messages: list[Any]) -> int:
    """Merge consecutive UserMessage→UserMessage and AssistantMessage→
    AssistantMessage pairs in place. Returns count merged.

    `ToolResultMessage` is never merged — providers expect tool_result
    blocks paired exactly with their tool_use ids. We treat it as its
    own role for this purpose."""
    if len(messages) < 2:
        return 0
    out: list[Any] = []
    merged = 0
    for msg in messages:
        if not out:
            out.append(msg)
            continue
        prev = out[-1]
        if _is_user_pair(prev, msg):
            out[-1] = _merge_user(prev, msg)
            merged += 1
            continue
        if _is_assistant_pair(prev, msg):
            out[-1] = _merge_assistant(prev, msg)
            merged += 1
            continue
        out.append(msg)
    if merged:
        messages[:] = out
        logger.info(
            "message-merge: coalesced %d consecutive same-role pair(s)",
            merged,
        )
    return merged


def _is_user_pair(a: Any, b: Any) -> bool:
    return isinstance(a, UserMessage) and isinstance(b, UserMessage)


def _is_assistant_pair(a: Any, b: Any) -> bool:
    return (
        isinstance(a, AssistantMessage)
        and isinstance(b, AssistantMessage)
        and not isinstance(a, ToolResultMessage)
        and not isinstance(b, ToolResultMessage)
    )


def _merge_user(a: UserMessage, b: UserMessage) -> UserMessage:
    """Merge two user messages.

    Both-string → join with newlines. Mixed string/list → coerce to
    list of typed blocks so the merger preserves images.
    """
    a_blocks = _user_to_blocks(a.content)
    b_blocks = _user_to_blocks(b.content)
    # Insert a separator text block between the two so the model sees
    # them as distinct user utterances within the merged turn.
    if a_blocks and b_blocks:
        merged: list[Any] = list(a_blocks)
        merged.append(TextContent(text="---"))
        merged.extend(b_blocks)
    else:
        merged = list(a_blocks) + list(b_blocks)
    # If the merge has no images, collapse back to plain str shape so
    # we don't bloat the message format unnecessarily.
    if all(isinstance(blk, TextContent) for blk in merged):
        text = "\n\n".join(blk.text for blk in merged if blk.text)
        return UserMessage(content=text)
    return UserMessage(content=merged)


def _merge_assistant(a: AssistantMessage, b: AssistantMessage) -> AssistantMessage:
    """Merge two assistant messages by concatenating content lists.

    `stop_reason` of the LATER message wins (it's the model's final
    state). `usage` is summed when both have it."""
    new_content = list(a.content) + list(b.content)
    usage: dict[str, Any] | None = None
    if a.usage or b.usage:
        usage = {}
        for d in (a.usage or {}, b.usage or {}):
            for k, v in d.items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
                elif k not in usage:
                    usage[k] = v
    return AssistantMessage(
        content=new_content,
        stop_reason=b.stop_reason or a.stop_reason,
        usage=usage,
    )


def _user_to_blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        if not content:
            return []
        return [TextContent(text=content)]
    if isinstance(content, list):
        return list(content)
    return []


__all__ = ["merge_consecutive_same_role"]
