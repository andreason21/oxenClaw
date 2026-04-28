"""Stop-reason recovery — re-emit the turn when the model gave us nothing.

A model that streams `StopEvent(end_turn)` with no text deltas (or a
provider that surfaces `stop_reason="safety"`/"refusal"/"sensitive")
leaves the user staring at an empty reply. This module decides whether
the assembled message is a "recoverable empty" and yields a nudge
message that the run loop appends before retrying.

Mirrors openclaw `attempt.stop-reason-recovery.ts`. Simpler — that one
also handles content-policy violations with provider-specific decoded
codes; we treat them all uniformly as "ask the model to retry in
plain language".
"""

from __future__ import annotations

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolUseBlock,
    UserMessage,
)

# Stop reasons that count as "model refused / declined to answer".
RECOVERABLE_STOP_REASONS: frozenset[str] = frozenset(
    {
        "refusal",
        "safety",
        "sensitive",
        "content_filter",
        "blocked",
    }
)


def is_recoverable_empty(message: AssistantMessage) -> bool:
    """A turn is "recoverable empty" when:
      - stop_reason is one of the refusal-family codes, OR
      - stop_reason is end_turn but the content has no text AND no
        tool_use blocks (the model genuinely streamed nothing).

    Tool-use turns are NEVER recoverable here — those are normal
    intermediate steps and the run loop's outer iteration handles them.
    """
    if any(isinstance(b, ToolUseBlock) for b in message.content):
        return False
    has_text = any(isinstance(b, TextContent) and (b.text or "").strip() for b in message.content)
    if has_text:
        return False
    if message.stop_reason in RECOVERABLE_STOP_REASONS:
        return True
    if message.stop_reason in (None, "end_turn", "stop"):
        return True
    return False


def build_recovery_nudge(stop_reason: str | None) -> UserMessage:
    """Return a synthetic user turn that nudges the model to retry.

    Phrased as a user-side meta-instruction so the model treats it as
    new input rather than a system override (small models occasionally
    refuse to revise system instructions but happily comply with a
    user re-ask)."""
    if stop_reason in RECOVERABLE_STOP_REASONS:
        body = (
            "Your previous reply was filtered as "
            f"`{stop_reason}`. Please retry — answer in plain "
            "language, no policy commentary, and reuse any context "
            "already provided. If a tool would help, call it."
        )
    else:
        body = (
            "Your previous reply was empty. Please retry and answer "
            "directly. If a tool (memory_search, weather, web_search, "
            "get_time, etc.) would help, call it."
        )
    return UserMessage(content=body)


__all__ = [
    "RECOVERABLE_STOP_REASONS",
    "build_recovery_nudge",
    "is_recoverable_empty",
]
