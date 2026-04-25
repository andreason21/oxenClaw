"""Conversation compaction pipeline.

Mirrors `pi-embedded-runner/compact.ts` + `compaction-hooks.ts` +
`compaction-runtime-context.ts` + `compact-reasons.ts` +
`manual-compaction-boundary.ts` + `run.overflow-compaction.*` from openclaw.

Algorithm:
1. The run loop tracks `usage_total["input_tokens"]` (or estimate via
   `estimate_tokens`) against `model.context_window`.
2. When the ratio crosses `compaction_threshold_ratio` (~0.85), the loop
   calls `maybe_compact()`. If a compaction is warranted, the function:
   - Picks a *boundary*: keep the last K turns verbatim, summarise the rest.
   - Calls a `summarizer_fn(messages)` (sub-LLM call) to produce a single
     SystemMessage that stands in for the dropped tail.
   - Records a `CompactionEntry` with the dropped index range, before/after
     token counts, and a `reason` flag (`auto`/`overflow`/`manual`/`timeout`).
3. The session manager persists the compaction history alongside the
   transcript so replay can reconstruct.

Phase 5 ships the algorithm + boundary picker + summarisation hook
contract. Phase 6 wires the compactions into AgentSession persistence.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sampyclaw.pi.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from sampyclaw.pi.session import AgentSession, CompactionEntry
from sampyclaw.pi.tokens import estimate_tokens
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.compaction")


# A summariser takes the messages-to-be-summarised and returns a string
# that will become the body of a SystemMessage placed at the boundary.
SummarizerFn = Callable[[list[AgentMessage]], Awaitable[str]]


CompactReason = str  # "auto" | "manual" | "overflow" | "timeout" | "boundary"


@dataclass(frozen=True)
class CompactionPlan:
    """What `decide_compaction` recommends."""

    needed: bool
    keep_tail_count: int
    drop_indexes: tuple[int, ...]
    reason: CompactReason
    tokens_before: int
    tokens_after_estimated: int


def _is_user_or_assistant_pair_index(
    messages: list[AgentMessage], idx: int
) -> bool:
    """An index that starts a user→assistant pair is a safe split boundary.
    Avoid splitting between assistant tool_use and the matching tool_result."""
    if idx <= 0 or idx >= len(messages):
        return True
    prev = messages[idx - 1]
    cur = messages[idx]
    if isinstance(prev, AssistantMessage):
        # If the previous assistant requested a tool, the next message is
        # the tool result — splitting between them orphans the tool call.
        from sampyclaw.pi.messages import ToolUseBlock

        has_pending_tool = any(
            isinstance(b, ToolUseBlock) for b in prev.content
        )
        if has_pending_tool and isinstance(cur, ToolResultMessage):
            return False
    return True


def decide_compaction(
    messages: list[AgentMessage],
    *,
    model_context_tokens: int,
    threshold_ratio: float = 0.85,
    keep_tail_turns: int = 6,
    reason: CompactReason = "auto",
    force: bool = False,
) -> CompactionPlan:
    """Decide whether to compact and where to split.

    Returns a `CompactionPlan` with `needed=False` if either:
    - the conversation is shorter than `keep_tail_turns + 1` (no room to
      compact), or
    - token usage is below `threshold_ratio * model_context_tokens` and
      `force=False`.
    """
    if not messages:
        return CompactionPlan(False, 0, (), reason, 0, 0)
    tokens_now = estimate_tokens(messages)
    threshold = int(model_context_tokens * threshold_ratio)
    if not force and tokens_now < threshold:
        return CompactionPlan(False, 0, (), reason, tokens_now, tokens_now)

    # Find a safe boundary: walk back from the tail keep_tail_turns turns.
    n = len(messages)
    tail_start = max(1, n - keep_tail_turns)
    while tail_start < n and not _is_user_or_assistant_pair_index(
        messages, tail_start
    ):
        tail_start += 1
    if tail_start >= n:
        # Nothing safely droppable.
        return CompactionPlan(False, len(messages), (), reason, tokens_now, tokens_now)

    drop = tuple(range(tail_start))
    # Estimate after-compaction tokens: assume the summary is ~10% of
    # the dropped block's tokens, plus the kept tail.
    dropped_tokens = sum(
        estimate_tokens([messages[i]]) for i in drop  # type: ignore[list-item]
    )
    kept_tokens = tokens_now - dropped_tokens
    estimated_after = kept_tokens + max(200, dropped_tokens // 10)
    return CompactionPlan(
        needed=True,
        keep_tail_count=n - tail_start,
        drop_indexes=drop,
        reason=reason,
        tokens_before=tokens_now,
        tokens_after_estimated=estimated_after,
    )


async def apply_compaction(
    messages: list[AgentMessage],
    plan: CompactionPlan,
    summarizer: SummarizerFn,
) -> tuple[list[AgentMessage], CompactionEntry]:
    """Execute a CompactionPlan: summarise dropped messages, splice in a
    SystemMessage. Returns (new_messages, entry)."""
    if not plan.needed:
        return list(messages), CompactionEntry(
            id=uuid4().hex,
            summary="",
            replaced_message_indexes=(),
            created_at=time.time(),
            reason=plan.reason,
            tokens_before=plan.tokens_before,
            tokens_after=plan.tokens_before,
        )

    dropped = [messages[i] for i in plan.drop_indexes]
    summary_text = await summarizer(dropped)
    summary_msg = SystemMessage(
        content=f"[COMPACTED SUMMARY of {len(dropped)} prior messages]\n{summary_text}"
    )
    if plan.drop_indexes:
        tail = messages[max(plan.drop_indexes) + 1 :]
    else:
        tail = messages
    new_messages: list[AgentMessage] = [summary_msg, *tail]
    tokens_after = estimate_tokens(new_messages)
    entry = CompactionEntry(
        id=uuid4().hex,
        summary=summary_text,
        replaced_message_indexes=plan.drop_indexes,
        created_at=time.time(),
        reason=plan.reason,
        tokens_before=plan.tokens_before,
        tokens_after=tokens_after,
    )
    logger.info(
        "compacted %d messages: %d → %d tokens (reason=%s)",
        len(dropped),
        plan.tokens_before,
        tokens_after,
        plan.reason,
    )
    return new_messages, entry


async def maybe_compact(
    session: AgentSession,
    *,
    model_context_tokens: int,
    summarizer: SummarizerFn,
    threshold_ratio: float = 0.85,
    keep_tail_turns: int = 6,
    reason: CompactReason = "auto",
    force: bool = False,
) -> bool:
    """Compact `session` in-place if needed. Returns True if compacted."""
    plan = decide_compaction(
        session.messages,
        model_context_tokens=model_context_tokens,
        threshold_ratio=threshold_ratio,
        keep_tail_turns=keep_tail_turns,
        reason=reason,
        force=force,
    )
    if not plan.needed:
        return False
    new_messages, entry = await apply_compaction(
        session.messages, plan, summarizer
    )
    session.messages = new_messages
    session.compactions.append(entry)
    return True


# ─── Default summariser ──────────────────────────────────────────────


async def truncating_summarizer(messages: list[AgentMessage]) -> str:
    """Cheap fallback summariser: keep first user turn + last assistant
    turn verbatim, drop the rest. Useful when no LLM-based summariser is
    available; the run loop can replace this with a real one."""
    if not messages:
        return ""
    first_user = next(
        (m for m in messages if isinstance(m, UserMessage)), None
    )
    last_assistant = next(
        (m for m in reversed(messages) if isinstance(m, AssistantMessage)),
        None,
    )
    parts: list[str] = [f"({len(messages)} messages compacted)"]
    if first_user:
        if isinstance(first_user.content, str):
            parts.append(f"First user turn: {first_user.content[:300]}")
        else:
            parts.append("First user turn: (multi-block)")
    if last_assistant:
        text_blocks = [
            b.text for b in last_assistant.content if hasattr(b, "text")
        ]
        if text_blocks:
            parts.append(f"Last assistant turn: {text_blocks[0][:300]}")
    return "\n".join(parts)


__all__ = [
    "CompactReason",
    "CompactionPlan",
    "SummarizerFn",
    "apply_compaction",
    "decide_compaction",
    "maybe_compact",
    "truncating_summarizer",
]
