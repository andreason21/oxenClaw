"""Incomplete-turn detection + cleanup.

Mirrors openclaw `pi-embedded-runner/run/incomplete-turn.ts`. When the
gateway crashes mid-turn (or a turn aborts on timeout / yield), the
persisted history can end up in one of two unhealthy states:

  1. **Trailing user**: the user message was saved but the assistant
     never replied. Next chat.send appends ANOTHER user message and
     the model sees two user turns in a row — most providers reject
     that. Fix: drop the trailing user turn so the new send produces
     a clean pair.
  2. **Tool-use without tool-result**: the assistant emitted a
     `tool_use` block but the run aborted before the result could be
     paired. The next attempt sends a tool_use with no matching
     tool_result → provider 400. Fix: append a synthetic
     "interrupted" tool_result so the chain is balanced.

Run at session-load time (`PiAgent._ensure_session`) BEFORE any new
user message is appended.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.incomplete_turn")


_INTERRUPTED_TOOL_RESULT_TEXT = (
    "(tool execution interrupted: previous turn aborted before this "
    "tool finished. Re-emit the call if you still need the result.)"
)


def repair_incomplete_turn(messages: list[Any]) -> dict[str, int]:
    """Repair `messages` in place. Returns counters for logging."""
    counters = {
        "trailing_users_dropped": 0,
        "synthetic_tool_results_added": 0,
    }
    if not messages:
        return counters

    # Pass 1: drop trailing UserMessages first. A user message that
    # appears AFTER an orphan tool_use is a re-typed prompt from the
    # operator after a crash — we drop it so the next chat.send can
    # append it cleanly. Doing this BEFORE the synthetic tool_result
    # pass also avoids the synthetic block ending up between two user
    # turns. Done in a backward walk so multiple trailing users
    # collapse together.
    while messages and isinstance(messages[-1], UserMessage):
        messages.pop()
        counters["trailing_users_dropped"] += 1
    if counters["trailing_users_dropped"]:
        logger.info(
            "incomplete-turn: dropped %d trailing user turn(s) with no assistant reply",
            counters["trailing_users_dropped"],
        )

    # Pass 2: balance unmatched tool_use blocks by appending a
    # synthetic ToolResultMessage. We walk forward and track which
    # tool_use ids have been answered.
    answered: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolResultMessage):
            for r in msg.results:
                if isinstance(r, ToolResultBlock):
                    answered.add(r.tool_use_id)
    pending_blocks: list[ToolResultBlock] = []
    for msg in messages:
        if not isinstance(msg, AssistantMessage):
            continue
        for b in msg.content:
            if isinstance(b, ToolUseBlock) and b.id not in answered:
                pending_blocks.append(
                    ToolResultBlock(
                        tool_use_id=b.id,
                        content=_INTERRUPTED_TOOL_RESULT_TEXT,
                        is_error=True,
                    )
                )
                answered.add(b.id)
    if pending_blocks:
        # Append ONE synthetic ToolResultMessage carrying every
        # unmatched id so later iterations see a clean chain.
        messages.append(ToolResultMessage(results=pending_blocks))
        counters["synthetic_tool_results_added"] = len(pending_blocks)
        logger.info(
            "incomplete-turn: appended %d synthetic tool_result(s) "
            "to balance unmatched tool_use blocks",
            len(pending_blocks),
        )
    return counters


__all__ = ["repair_incomplete_turn"]
