"""Replay-time tool-call sanitiser.

Mirrors openclaw `pi-embedded-runner/run/attempt.ts:649-848`
(`sanitizeReplayToolCallInputs`, commit `c3972982b5`). When a session is
reloaded from disk — SQLite, ACP transcript, dashboard rehydrate — the
persisted `ToolUseBlock`s may have been written mid-stream by a prior
crashed run, leaving:

  - empty / missing `id`
  - missing `input` (model never finished streaming arguments)
  - garbage `name`: whitespace inside, > 64 chars, not registered

If we forward those to the provider as-is, the provider rejects the
whole transcript with a 400 ("malformed tool_use") and the user can't
resume the conversation.

The sanitiser walks the message list, drops malformed blocks from each
assistant message, and drops the assistant message entirely when no
content survives. Idempotent on well-formed input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from oxenclaw.pi.messages import AssistantMessage, ToolUseBlock
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.replay_sanitizer")

# Match upstream `REPLAY_TOOL_CALL_NAME_MAX_CHARS = 64`.
_REPLAY_TOOL_NAME_MAX = 64
_WHITESPACE_RE = re.compile(r"\s")


@dataclass(frozen=True)
class ReplaySanitizeReport:
    """Summary of what the sanitiser changed."""

    dropped_blocks: int
    dropped_assistant_messages: int


def _is_well_formed(block: ToolUseBlock, allowed_tool_names: set[str] | None) -> bool:
    """True when the block can safely be sent back to a provider.

    Bar:
      - non-empty `id` (required for tool_result correlation)
      - `input` is a dict (the model finished streaming arguments)
      - `name` is non-empty, ≤ 64 chars, no whitespace, and — if a
        registry was supplied — present in it
    """
    if not isinstance(block.id, str) or not block.id.strip():
        return False
    if not isinstance(block.input, dict):
        return False
    name = block.name if isinstance(block.name, str) else ""
    name = name.strip()
    if not name or len(name) > _REPLAY_TOOL_NAME_MAX:
        return False
    if _WHITESPACE_RE.search(name):
        return False
    if allowed_tool_names and name not in allowed_tool_names:
        return False
    return True


def sanitize_replay_tool_calls(
    messages: list[Any],
    *,
    allowed_tool_names: set[str] | None = None,
) -> ReplaySanitizeReport:
    """Walk `messages` in place; drop malformed ToolUseBlocks.

    When an AssistantMessage's content list ends up empty after the
    drop, the message itself is removed — sending an assistant turn
    with zero content blocks would itself be a 400.
    """
    dropped_blocks = 0
    to_remove: list[int] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, AssistantMessage):
            continue
        if not isinstance(msg.content, list):
            continue
        new_content: list[Any] = []
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                if not _is_well_formed(block, allowed_tool_names):
                    dropped_blocks += 1
                    continue
            new_content.append(block)
        if len(new_content) != len(msg.content):
            if not new_content:
                to_remove.append(idx)
            else:
                msg.content = new_content
    # Remove emptied assistant messages from tail to head so indices stay valid.
    for idx in reversed(to_remove):
        messages.pop(idx)
    if dropped_blocks or to_remove:
        logger.info(
            "replay sanitizer: dropped %d malformed tool_use block(s) and %d empty assistant turn(s)",
            dropped_blocks,
            len(to_remove),
        )
    return ReplaySanitizeReport(
        dropped_blocks=dropped_blocks,
        dropped_assistant_messages=len(to_remove),
    )


__all__ = [
    "ReplaySanitizeReport",
    "sanitize_replay_tool_calls",
]
