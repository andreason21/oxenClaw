"""Smart per-tool-result truncation, context-window aware.

Mirrors openclaw `pi-embedded-runner/tool-result-truncation.ts:1-360`
(which the upstream context guard at `tool-result-context-guard.ts`
also leans on). Two responsibilities:

  1. **Smart string-level truncation** — `truncate_tool_result_text`
     uses a head+tail strategy when the tail likely contains errors,
     JSON closing structure, or summary lines, so important content
     near the end isn't lost on a long output.

  2. **Context-window aware budgets** — `calculate_max_tool_result_chars`
     resolves a per-result char budget from the model's context window
     (30 % share, capped at 400_000 chars / ~100K tokens). A 9 GB HTML
     web_fetch into a 32K-context model is clipped to ~10K tokens; a
     200K-context model gets ~60K, while a 2M-context tier still tops
     out at 400K so a single tool can't blow >100K tokens of context
     in one shot.

The cap is intentionally per-result, not per-session. Cumulative
session-level pressure is the existing `preemptive_compaction` /
`OpenclawContextEngine` job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from oxenclaw.pi.messages import TextContent, ToolResultBlock, ToolResultMessage
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.tool_result_truncation")

# Identical to openclaw upstream — keep the constants pinned so a
# future drift is easy to spot.
MAX_TOOL_RESULT_CONTEXT_SHARE = 0.3
HARD_MAX_TOOL_RESULT_CHARS = 400_000
MIN_KEEP_CHARS = 2_000

_CHARS_PER_TOKEN = 4  # rough English-text estimate

TRUNCATION_SUFFIX = (
    "\n\n⚠️ [Content truncated — original was too large for the model's "
    "context window. The content above is a partial view. If you need "
    "more, request specific sections or use offset/limit parameters to "
    "read smaller chunks.]"
)
MIDDLE_OMISSION_MARKER = "\n\n⚠️ [... middle content omitted — showing head and tail ...]\n\n"

_IMPORTANT_TAIL_PATTERN = re.compile(
    r"\b(error|exception|failed|fatal|traceback|panic|stack trace|errno|exit code"
    r"|total|summary|result|complete|finished|done)\b",
    re.IGNORECASE,
)
_JSON_TAIL_PATTERN = re.compile(r"\}\s*$")


def _has_important_tail(text: str) -> bool:
    """True when the last ~2K chars look like the part the user must
    not lose (errors, summary, JSON closing). Keeps both head and tail
    in that case at the cost of dropping middle content."""
    tail = text[-2000:]
    if _IMPORTANT_TAIL_PATTERN.search(tail):
        return True
    if _JSON_TAIL_PATTERN.search(tail.strip()):
        return True
    return False


def truncate_tool_result_text(
    text: str,
    max_chars: int,
    *,
    suffix: str = TRUNCATION_SUFFIX,
    min_keep_chars: int = MIN_KEEP_CHARS,
) -> str:
    """Trim `text` to fit `max_chars`, preserving error/summary tails when present.

    Cuts at newline boundaries near the budget so we don't slice
    mid-JSON-line / mid-stack-frame.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    # When the operator-supplied cap is tighter than `min_keep_chars`
    # (e.g. an explicit per-tool `max_result_chars=200`), drop the floor
    # to respect the tighter bound. The floor is a default safety, not
    # an override.
    if max_chars < min_keep_chars:
        min_keep_chars = max(0, max_chars - len(suffix))
    budget = max(min_keep_chars, max_chars - len(suffix))

    if _has_important_tail(text) and budget > min_keep_chars * 2:
        tail_budget = min(budget // 3, 4_000)
        head_budget = budget - tail_budget - len(MIDDLE_OMISSION_MARKER)
        if head_budget > min_keep_chars:
            head_cut = head_budget
            head_newline = text.rfind("\n", 0, head_budget)
            if head_newline > int(head_budget * 0.8):
                head_cut = head_newline
            tail_start = len(text) - tail_budget
            tail_newline = text.find("\n", tail_start)
            if tail_newline != -1 and tail_newline < tail_start + int(tail_budget * 0.2):
                tail_start = tail_newline + 1
            return text[:head_cut] + MIDDLE_OMISSION_MARKER + text[tail_start:] + suffix

    cut_point = budget
    last_newline = text.rfind("\n", 0, budget)
    if last_newline > int(budget * 0.8):
        cut_point = last_newline
    return text[:cut_point] + suffix


def calculate_max_tool_result_chars(context_window_tokens: int) -> int:
    """Per-result char budget from a context-window size in tokens.

    `min(30% of context, 400K chars)`. Below 1K tokens we still allow
    `MIN_KEEP_CHARS` so tiny test windows don't degenerate to zero.
    """
    safe_tokens = max(0, int(context_window_tokens * MAX_TOOL_RESULT_CONTEXT_SHARE))
    raw = safe_tokens * _CHARS_PER_TOKEN
    capped = min(raw, HARD_MAX_TOOL_RESULT_CHARS)
    return max(MIN_KEEP_CHARS, capped)


def _tool_result_text_length(msg: ToolResultMessage) -> int:
    """Total text-content length across all `ToolResultBlock`s in `msg`."""
    total = 0
    for r in msg.results:
        content = r.content
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, TextContent) and isinstance(b.text, str):
                    total += len(b.text)
    return total


def _truncate_block_in_place(block: ToolResultBlock, max_chars: int) -> bool:
    """Trim the block's text content if oversize. Returns True when changed."""
    content = block.content
    if isinstance(content, str):
        if len(content) <= max_chars:
            return False
        new_text = truncate_tool_result_text(content, max_chars)
        try:
            object.__setattr__(block, "content", new_text)
        except Exception:
            return False
        return True
    if isinstance(content, list):
        # Distribute the budget proportionally across text blocks.
        total = sum(
            len(b.text) for b in content if isinstance(b, TextContent) and isinstance(b.text, str)
        )
        if total <= max_chars:
            return False
        changed = False
        new_blocks: list[Any] = []
        for b in content:
            if isinstance(b, TextContent) and isinstance(b.text, str) and len(b.text) > 0:
                share = len(b.text) / max(1, total)
                block_budget = max(MIN_KEEP_CHARS, int(max_chars * share))
                new_text = truncate_tool_result_text(b.text, block_budget)
                if new_text != b.text:
                    new_blocks.append(TextContent(text=new_text))
                    changed = True
                    continue
            new_blocks.append(b)
        if changed:
            try:
                object.__setattr__(block, "content", new_blocks)
            except Exception:
                return False
        return changed
    return False


def truncate_tool_result_message(msg: ToolResultMessage, max_chars: int) -> int:
    """Trim oversize text content in `msg` in place. Returns block count touched."""
    if _tool_result_text_length(msg) <= max_chars:
        return 0
    touched = 0
    for r in msg.results:
        if _truncate_block_in_place(r, max_chars):
            touched += 1
    return touched


def truncate_oversized_tool_results_in_messages(
    messages: Iterable[Any],
    *,
    context_window_tokens: int,
) -> int:
    """Run a single in-place pass; returns number of result blocks trimmed.

    Idempotent on already-trimmed content. Safe to call every turn.
    """
    max_chars = calculate_max_tool_result_chars(context_window_tokens)
    touched = 0
    for msg in messages:
        if isinstance(msg, ToolResultMessage):
            touched += truncate_tool_result_message(msg, max_chars)
    if touched:
        logger.info(
            "tool-result-truncation: trimmed %d block(s) at %d chars (window=%d tok)",
            touched,
            max_chars,
            context_window_tokens,
        )
    return touched


def session_likely_has_oversized_tool_results(
    messages: Iterable[Any],
    *,
    context_window_tokens: int,
) -> bool:
    """Cheap heuristic for the `compress-then-retry` decider.

    Lets the run loop know there's work for the truncator before
    spinning the more-expensive `preemptive_compaction` path.
    """
    max_chars = calculate_max_tool_result_chars(context_window_tokens)
    for msg in messages:
        if isinstance(msg, ToolResultMessage) and _tool_result_text_length(msg) > max_chars:
            return True
    return False


__all__ = [
    "HARD_MAX_TOOL_RESULT_CHARS",
    "MAX_TOOL_RESULT_CONTEXT_SHARE",
    "MIN_KEEP_CHARS",
    "calculate_max_tool_result_chars",
    "session_likely_has_oversized_tool_results",
    "truncate_oversized_tool_results_in_messages",
    "truncate_tool_result_message",
    "truncate_tool_result_text",
]
