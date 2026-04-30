"""Preemptive compaction — shrink the prompt BEFORE sending it.

The legacy `LegacyContextEngine.compact` runs AFTER a turn, so the
turn that finally pushes context past the model's window is already
doomed (provider returns 400 / silent truncation). This module checks
the prompt size up front and decides whether to:

  - `noop` — fits within budget, send as-is.
  - `truncate_tool_results_only` — recent tool_result chunks are
    largest; prune them first since older ones rarely matter.
  - `compact_then_send` — caller should run a full compaction pass
    before retrying (delegated back to ContextEngine).

We use the rough heuristic openclaw uses for cheap inline estimation:
~4 chars per token. That's pessimistic for English (closer to 3.5)
and accurate enough for Korean (multi-byte chars ≈ 1 token each).
The estimator deliberately over-counts; the goal is "don't blow the
window" not "perfect accounting".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    UserMessage,
)

# Soft over-estimate; we'd rather compact one turn early than hit a 400.
# Family-aware ratios live in `token_estimator.chars_per_token_for(model_id)`;
# this constant remains as a safe default when the model id is unknown.
DEFAULT_CHARS_PER_TOKEN = 3.5


class CompactionRoute(StrEnum):
    NOOP = "noop"
    TRUNCATE_TOOL_RESULTS = "truncate_tool_results_only"
    COMPACT_THEN_SEND = "compact_then_send"


@dataclass
class CompactionDecision:
    route: CompactionRoute
    estimated_prompt_tokens: int
    prompt_budget_tokens: int
    overflow_tokens: int
    tool_result_reducible_chars: int


def _estimate_tokens_from_text(text: str, *, chars_per_token: float) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))


def _estimate_message_tokens(msg: Any, *, chars_per_token: float) -> int:
    """Rough token count for a single message."""
    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, str):
            return _estimate_tokens_from_text(content, chars_per_token=chars_per_token)
        total = 0
        for b in content:
            if isinstance(b, TextContent):
                total += _estimate_tokens_from_text(b.text, chars_per_token=chars_per_token)
        return total
    if isinstance(msg, AssistantMessage):
        total = 0
        for b in msg.content:
            if isinstance(b, TextContent):
                total += _estimate_tokens_from_text(b.text, chars_per_token=chars_per_token)
            else:
                total += 16  # placeholder per non-text block
        return total
    if isinstance(msg, ToolResultMessage):
        total = 0
        for r in msg.results:
            if isinstance(r, ToolResultBlock):
                total += _estimate_tokens_from_text(
                    r.content or "", chars_per_token=chars_per_token
                )
        return total
    return 0


def estimate_prompt_tokens(
    *,
    system: str | None,
    messages: list[Any],
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> int:
    total = _estimate_tokens_from_text(system or "", chars_per_token=chars_per_token)
    for m in messages:
        total += _estimate_message_tokens(m, chars_per_token=chars_per_token)
    return total


def _tool_result_reducible_chars(messages: list[Any]) -> int:
    total = 0
    for m in messages:
        if isinstance(m, ToolResultMessage):
            for r in m.results:
                content = r.content or ""
                if len(content) > 1024:
                    # We could trim everything past the first 1024 chars.
                    total += len(content) - 1024
    return total


def decide(
    *,
    system: str | None,
    messages: list[Any],
    context_window: int,
    threshold_ratio: float = 0.85,
    reserve_tokens: int = 1024,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> CompactionDecision:
    """Decide whether and how to compact before sending.

    `threshold_ratio` is the fraction of the model's context window we
    aim to fit under. `reserve_tokens` is held back for the model's
    response. So usable budget = context_window * threshold_ratio - reserve.
    """
    estimated = estimate_prompt_tokens(
        system=system, messages=messages, chars_per_token=chars_per_token
    )
    budget = max(1, int(context_window * threshold_ratio) - reserve_tokens)
    if estimated <= budget:
        return CompactionDecision(
            route=CompactionRoute.NOOP,
            estimated_prompt_tokens=estimated,
            prompt_budget_tokens=budget,
            overflow_tokens=0,
            tool_result_reducible_chars=0,
        )
    overflow = estimated - budget
    reducible = _tool_result_reducible_chars(messages)
    reducible_tokens = int(reducible / chars_per_token)
    if reducible_tokens >= overflow:
        return CompactionDecision(
            route=CompactionRoute.TRUNCATE_TOOL_RESULTS,
            estimated_prompt_tokens=estimated,
            prompt_budget_tokens=budget,
            overflow_tokens=overflow,
            tool_result_reducible_chars=reducible,
        )
    return CompactionDecision(
        route=CompactionRoute.COMPACT_THEN_SEND,
        estimated_prompt_tokens=estimated,
        prompt_budget_tokens=budget,
        overflow_tokens=overflow,
        tool_result_reducible_chars=reducible,
    )


def truncate_tool_results(messages: list[Any], *, keep_chars: int = 1024) -> int:
    """Trim every tool_result content past `keep_chars`. Returns count
    of bytes removed across the message list (so callers can log)."""
    removed = 0
    for m in messages:
        if isinstance(m, ToolResultMessage):
            for r in m.results:
                content = r.content or ""
                if len(content) > keep_chars:
                    new_content = content[:keep_chars] + "\n[...truncated]"
                    removed += len(content) - len(new_content)
                    # ToolResultBlock is a frozen dataclass so we mutate
                    # carefully — fall back to attribute set when possible.
                    try:
                        object.__setattr__(r, "content", new_content)
                    except Exception:
                        pass
    return removed


__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "CompactionDecision",
    "CompactionRoute",
    "decide",
    "estimate_prompt_tokens",
    "truncate_tool_results",
]
