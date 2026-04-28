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

This module also ships an LLM-based structured summariser pipeline
(``llm_structured_summarizer`` + ``structured_summarizer_pipeline``) that
mirrors hermes-agent's Phase-2 quality-leap compactor: a 12-section
template, tool-result deduplication / arg truncation / orphan repair,
and a `CompactionGuard` anti-thrashing helper.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from oxenclaw.pi.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from oxenclaw.pi.session import AgentSession, CompactionEntry
from oxenclaw.pi.tokens import estimate_tokens
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.compaction")


# A summariser takes the messages-to-be-summarised and returns a string
# that will become the body of a SystemMessage placed at the boundary.
SummarizerFn = Callable[[list[AgentMessage]], Awaitable[str]]


CompactReason = str  # "auto" | "manual" | "overflow" | "timeout" | "boundary"


# ─── Structured summariser preamble + template ─────────────────────────


SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Your current task is identified in the '## Active Task' section of the "
    "summary — resume exactly from there. "
    "Respond ONLY to the latest user message that appears AFTER this "
    "summary. The current session state (files, config, etc.) may reflect "
    "work described here — avoid repeating it:"
)


_SUMMARIZER_PREAMBLE = (
    "You are a summarization agent creating a context checkpoint. "
    "Your output will be injected as reference material for a DIFFERENT "
    "assistant that continues the conversation. "
    "Do NOT respond to any questions or requests in the conversation — "
    "only output the structured summary. "
    "Do NOT include any preamble, greeting, or prefix. "
    "Write the summary in the same language the user was using in the "
    "conversation — do not translate or switch to English. "
    "NEVER include API keys, tokens, passwords, secrets, credentials, "
    "or connection strings in the summary — replace any that appear "
    "with [REDACTED]."
)


_TEMPLATE_SECTIONS = """## Active Task
[THE SINGLE MOST IMPORTANT FIELD. Copy the user's most recent request or
task assignment verbatim. If multiple tasks were requested and only some
are done, list only the ones NOT yet completed. The next assistant must
pick up exactly here. If no outstanding task exists, write "None."]

## Goal
[What the user is trying to accomplish overall]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and
outcome. Format each as: N. ACTION target — outcome [tool: name]
Be specific with file paths, commands, line numbers, and results.]

## Active State
[Current working state — files, branches, tests, running processes, env]

## In Progress
[Work currently underway — what was being done when compaction fired]

## Blocked
[Any blockers, errors, or issues not yet resolved. Include exact errors.]

## Key Decisions
[Important technical decisions and WHY they were made]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer]

## Pending User Asks
[Questions or requests from the user that have NOT yet been answered. If
none, write "None."]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Remaining Work
[What remains to be done — framed as context, not instructions]

## Critical Context
[Any specific values, error messages, configuration details, or data that
would be lost without explicit preservation. NEVER include API keys,
tokens, passwords, or credentials — write [REDACTED] instead.]

Write only the summary body. Do not include any preamble or prefix."""


@dataclass(frozen=True)
class CompactionPlan:
    """What `decide_compaction` recommends."""

    needed: bool
    keep_tail_count: int
    drop_indexes: tuple[int, ...]
    reason: CompactReason
    tokens_before: int
    tokens_after_estimated: int


def _is_user_or_assistant_pair_index(messages: list[AgentMessage], idx: int) -> bool:
    """An index that starts a user→assistant pair is a safe split boundary.
    Avoid splitting between assistant tool_use and the matching tool_result."""
    if idx <= 0 or idx >= len(messages):
        return True
    prev = messages[idx - 1]
    cur = messages[idx]
    if isinstance(prev, AssistantMessage):
        # If the previous assistant requested a tool, the next message is
        # the tool result — splitting between them orphans the tool call.
        from oxenclaw.pi.messages import ToolUseBlock as _TUB

        has_pending_tool = any(isinstance(b, _TUB) for b in prev.content)
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
    guard: CompactionGuard | None = None,
    guard_threshold_pct: float = 10.0,
) -> CompactionPlan:
    """Decide whether to compact and where to split.

    Returns a `CompactionPlan` with `needed=False` if any of:
    - the conversation is shorter than `keep_tail_turns + 1`,
    - token usage is below `threshold_ratio * model_context_tokens` and
      ``force=False``, or
    - the optional ``guard`` says recent passes are not making progress.
    """
    if not messages:
        return CompactionPlan(False, 0, (), reason, 0, 0)
    tokens_now = estimate_tokens(messages)
    threshold = int(model_context_tokens * threshold_ratio)
    if not force and tokens_now < threshold:
        return CompactionPlan(False, 0, (), reason, tokens_now, tokens_now)
    # Anti-thrash: bail when recent passes barely saved anything.  Logged
    # at WARNING so operators can see the loop is bailing instead of
    # silently doing nothing.
    if guard is not None and should_skip_compaction(
        guard, tokens_now, threshold_pct=guard_threshold_pct
    ):
        logger.warning(
            "compaction guard: skipping pass — last 2 attempts each "
            "saved < %.1f%% (history=%s, current=%d)",
            guard_threshold_pct,
            guard.history,
            tokens_now,
        )
        return CompactionPlan(False, 0, (), reason, tokens_now, tokens_now)

    # Find a safe boundary: walk back from the tail keep_tail_turns turns.
    n = len(messages)
    tail_start = max(1, n - keep_tail_turns)
    while tail_start < n and not _is_user_or_assistant_pair_index(messages, tail_start):
        tail_start += 1
    if tail_start >= n:
        # Nothing safely droppable.
        return CompactionPlan(False, len(messages), (), reason, tokens_now, tokens_now)

    drop = tuple(range(tail_start))
    # Estimate after-compaction tokens: assume the summary is ~10% of
    # the dropped block's tokens, plus the kept tail.
    dropped_tokens = sum(
        estimate_tokens([messages[i]])
        for i in drop  # type: ignore[list-item]
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
    tail = messages[max(plan.drop_indexes) + 1 :] if plan.drop_indexes else messages
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
    new_messages, entry = await apply_compaction(session.messages, plan, summarizer)
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
    first_user = next((m for m in messages if isinstance(m, UserMessage)), None)
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
        text_blocks = [b.text for b in last_assistant.content if hasattr(b, "text")]
        if text_blocks:
            parts.append(f"Last assistant turn: {text_blocks[0][:300]}")
    return "\n".join(parts)


# ─── Helpers for the structured summariser pipeline ─────────────────


def _summarize_tool_result(name: str, content: str) -> str:
    """Compress a tool result into one informative line.

    Replaces large tool outputs with structured one-liners during the
    pre-pass (cheap, no LLM call). Mirrors hermes
    `_summarize_tool_result` shape but covers oxenclaw tool names.
    """
    body = content or ""
    body_len = len(body)
    line_count = body.count("\n") + 1 if body.strip() else 0

    if name == "shell":
        return f"[shell] ran command -> {line_count} lines output, {body_len:,} chars"
    if name == "read_file":
        return f"[read_file] read file ({body_len:,} chars)"
    if name == "read_pdf":
        return f"[read_pdf] read pdf ({body_len:,} chars)"
    if name == "write_file":
        return f"[write_file] wrote file ({line_count} lines)"
    if name == "edit":
        return f"[edit] applied edit ({body_len:,} chars result)"
    if name == "list_dir":
        return f"[list_dir] listed dir ({line_count} entries)"
    if name == "grep":
        return f"[grep] searched ({line_count} lines result)"
    if name == "glob":
        return f"[glob] matched files ({line_count} matches)"
    if name == "search_files":
        return f"[search_files] content search ({line_count} matches)"
    if name == "memory_save":
        return f"[memory_save] saved fact ({body_len:,} chars)"
    if name == "memory_search":
        return f"[memory_search] recall ({line_count} hits, {body_len:,} chars)"
    if name == "memory_get":
        return f"[memory_get] read memory ({body_len:,} chars)"
    if name == "web_search":
        return f"[web_search] search ({body_len:,} chars result)"
    if name == "web_fetch":
        return f"[web_fetch] fetched URL ({body_len:,} chars)"
    if name == "weather":
        return f"[weather] {body[:80]}"
    if name == "get_time":
        return f"[get_time] {body[:80]}"
    if name == "github":
        return f"[github] called ({body_len:,} chars result)"
    if name == "wiki_search":
        return f"[wiki_search] vault search ({line_count} hits)"
    if name == "wiki_get":
        return f"[wiki_get] read page ({body_len:,} chars)"
    if name == "wiki_save":
        return "[wiki_save] wrote page"
    if name == "summarize":
        return f"[summarize] summarised ({body_len:,} chars)"
    if name == "skill_resolver":
        return f"[skill_resolver] resolved skill ({body_len:,} chars)"
    if name == "subagents":
        return f"[subagents] delegated ({body_len:,} chars result)"
    if name == "coding_agent":
        return f"[coding_agent] delegated ({body_len:,} chars result)"
    if name == "healthcheck":
        return f"[healthcheck] {body[:80]}"
    if name == "cron":
        return "[cron] scheduled job"
    if name == "process":
        return "[process] managed process"
    if name == "message":
        return f"[message] sent ({body_len:,} chars)"
    if name in (
        "browser_navigate",
        "browser_snapshot",
        "browser_screenshot",
        "browser_click",
        "browser_fill",
        "browser_evaluate",
        "browser_download",
    ):
        return f"[{name}] acted ({body_len:,} chars)"
    if name in (
        "sessions_yield",
        "sessions_spawn",
        "sessions_status",
        "sessions_list",
        "sessions_history",
        "sessions_send",
    ):
        return f"[{name}] session op ({body_len:,} chars)"
    if name == "session_logs":
        return f"[session_logs] read logs ({line_count} lines)"
    return f"[{name}] tool result ({body_len:,} chars)"


def _truncate_tool_call_args_json(args_json: str, max_chars: int = 4000) -> str:
    """Shrink long string leaves inside a tool-call args JSON blob while
    preserving JSON validity.

    A naive byte-truncate on the encoded JSON produces unterminated
    strings and invalid braces, which downstream providers reject with a
    non-retryable 400. This helper parses the blob, walks it
    recursively, and replaces oversized string leaves with a marker.
    Falls back to a JSON-safe placeholder if parsing fails.
    """
    if not args_json or len(args_json) <= max_chars:
        return args_json or ""

    try:
        parsed = json.loads(args_json)
    except (ValueError, TypeError):
        head = args_json[: min(max_chars, 200)]
        return json.dumps({"_truncated": True, "_raw_head": head})

    leaf_budget = max(64, max_chars // 4)

    def _shrink(obj: Any) -> Any:
        if isinstance(obj, str):
            if len(obj) > leaf_budget:
                return obj[:leaf_budget] + "...(truncated)"
            return obj
        if isinstance(obj, dict):
            return {k: _shrink(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_shrink(v) for v in obj]
        return obj

    shrunken = _shrink(parsed)
    return json.dumps(shrunken, ensure_ascii=False)


def _tool_result_body(block: ToolResultBlock) -> str:
    """Best-effort string view of a tool result block's content."""
    content = block.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            text = getattr(b, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return ""


def _set_tool_result_content(block: ToolResultBlock, new_content: str) -> None:
    """Mutate a ToolResultBlock's content in-place, honouring frozen models."""
    try:
        block.content = new_content  # type: ignore[misc]
    except Exception:
        try:
            object.__setattr__(block, "content", new_content)
        except Exception:
            pass


def _dedup_tool_results_by_md5(messages: list[AgentMessage]) -> int:
    """Md5-hash each tool result body and replace older duplicates with a
    back-reference. Returns count of dedup-replaced blocks.

    Walks the message list backwards so the most recent occurrence of
    any given content keeps its full body and earlier copies become
    short markers. Skips bodies < 200 chars where dedup gains are tiny.
    """
    seen: dict[str, str] = {}
    deduped = 0
    for msg in reversed(messages):
        if not isinstance(msg, ToolResultMessage):
            continue
        for block in msg.results:
            body = _tool_result_body(block)
            if len(body) < 200:
                continue
            digest = hashlib.md5(body.encode("utf-8", errors="replace")).hexdigest()[:12]
            if digest in seen:
                anchor = seen[digest]
                _set_tool_result_content(
                    block,
                    f"[dedup: same as earlier tool_result {anchor}]",
                )
                deduped += 1
            else:
                seen[digest] = block.tool_use_id
    return deduped


def _sanitize_tool_pairs(messages: list[AgentMessage]) -> int:
    """Repair orphaned tool_call / tool_result pairs in-place.

    Two failure modes after summarisation drops messages:
      1. A ToolResultBlock references a tool_use_id whose AssistantMessage
         was summarised away — the API rejects it.
      2. An assistant tool_use was kept but its matching tool_result was
         dropped — also rejected.

    This function:
      - Removes orphaned ToolResultBlocks (whose parent tool_use vanished).
      - Inserts stub ToolResultMessages for unresolved tool_uses.

    Returns the count of orphans repaired (removed + stubbed).
    """
    surviving_tool_use_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    surviving_tool_use_ids.add(b.id)

    result_ids: set[str] = set()
    for msg in messages:
        if isinstance(msg, ToolResultMessage):
            for r in msg.results:
                result_ids.add(r.tool_use_id)

    repaired = 0

    orphan_result_ids = result_ids - surviving_tool_use_ids
    if orphan_result_ids:
        for msg in list(messages):
            if isinstance(msg, ToolResultMessage):
                kept = [r for r in msg.results if r.tool_use_id not in orphan_result_ids]
                if len(kept) != len(msg.results):
                    repaired += len(msg.results) - len(kept)
                    if not kept:
                        messages.remove(msg)
                    else:
                        try:
                            msg.results = kept  # type: ignore[misc]
                        except Exception:
                            try:
                                object.__setattr__(msg, "results", kept)
                            except Exception:
                                pass

    missing_ids = surviving_tool_use_ids - result_ids
    if missing_ids:
        i = 0
        while i < len(messages):
            msg = messages[i]
            if isinstance(msg, AssistantMessage):
                pending = [
                    b for b in msg.content if isinstance(b, ToolUseBlock) and b.id in missing_ids
                ]
                if pending:
                    stub_blocks = [
                        ToolResultBlock(
                            tool_use_id=b.id,
                            content="[tool_result missing — call did not complete in this view]",
                            is_error=False,
                        )
                        for b in pending
                    ]
                    stub_msg = ToolResultMessage(results=stub_blocks)
                    messages.insert(i + 1, stub_msg)
                    repaired += len(pending)
                    i += 1
                    for b in pending:
                        missing_ids.discard(b.id)
            i += 1

    return repaired


def _align_boundary_backward(messages: list[AgentMessage], cut_idx: int) -> int:
    """Pull a compress-end boundary back to avoid splitting a
    tool_call / result group.

    If `messages[cut_idx-1]` is a tool_result, walk backward past
    consecutive tool_results to the parent assistant; if found, move
    the cut before the assistant so the whole group is compressed
    together.
    """
    if cut_idx <= 0 or cut_idx >= len(messages):
        return cut_idx
    check = cut_idx - 1
    while check >= 0 and isinstance(messages[check], ToolResultMessage):
        check -= 1
    if check >= 0 and isinstance(messages[check], AssistantMessage):
        has_tool_use = any(isinstance(b, ToolUseBlock) for b in messages[check].content)
        if has_tool_use:
            return check
    return cut_idx


def _ensure_last_user_message_in_tail(messages: list[AgentMessage], cut_idx: int) -> int:
    """Guarantee the most recent user message is at or after `cut_idx`.

    Mirrors hermes `_ensure_last_user_message_in_tail`. The last user
    message is the active-task signal — if it ends up in the compressed
    middle, the next assistant treats the user's request as already
    resolved. Walk `cut_idx` back to include it when missing.
    """
    if cut_idx >= len(messages):
        return cut_idx
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], UserMessage):
            last_user_idx = i
            break
    if last_user_idx < 0 or last_user_idx >= cut_idx:
        return cut_idx
    return max(0, last_user_idx)


# ─── CompactionGuard ─────────────────────────────────────────────────


@dataclass
class CompactionGuard:
    """Anti-thrashing guard for the compaction pipeline.

    Tracks the last 3 (before, after) token-count pairs. If the most
    recent two each saved less than `threshold_ratio` of the prompt,
    callers should bail out rather than re-summarise the same window.
    Mirrors hermes-agent's `_ineffective_compression_count`.
    """

    history: list[tuple[int, int]] = field(default_factory=list)
    threshold_ratio: float = 0.10

    def record(self, before: int, after: int) -> None:
        self.history.append((int(before), int(after)))
        if len(self.history) > 3:
            self.history = self.history[-3:]

    def reset(self) -> None:
        self.history.clear()

    def should_skip(self) -> bool:
        """Class-method form: skip when the last 2 entries each saved < threshold."""
        if len(self.history) < 2:
            return False
        last_two = self.history[-2:]
        for before, after in last_two:
            if before <= 0:
                return False
            saved_ratio = (before - after) / before
            if saved_ratio >= self.threshold_ratio:
                return False
        return True


def should_skip_compaction(
    guard: CompactionGuard,
    current_size: int,
    threshold_pct: float = 10.0,
) -> bool:
    """Free-function variant of :meth:`CompactionGuard.should_skip`.

    Returns True when the last 2 attempts saved < ``threshold_pct``
    each.  ``current_size`` is accepted for API compatibility with
    callers that may want to extend the heuristic later (e.g. always
    proceed when size has grown back past a threshold); the current
    implementation just defers to the recorded history.
    """
    _ = current_size  # reserved for future heuristics
    if len(guard.history) < 2:
        return False
    last_two = guard.history[-2:]
    threshold = max(0.0, threshold_pct) / 100.0
    for before, after in last_two:
        if before <= 0:
            return False
        saved_ratio = (before - after) / before
        if saved_ratio >= threshold:
            return False
    return True


# ─── LLM-based structured summariser ─────────────────────────────────


def _serialize_messages_for_summary(messages: list[AgentMessage]) -> str:
    """Render messages into role-tagged text for the summariser prompt."""
    parts: list[str] = []
    content_max = 6000
    head = 4000
    tail = 1500
    for m in messages:
        if isinstance(m, SystemMessage):
            text = m.content or ""
            if len(text) > content_max:
                text = text[:head] + "\n...[truncated]...\n" + text[-tail:]
            parts.append(f"[SYSTEM]: {text}")
            continue
        if isinstance(m, UserMessage):
            content = m.content
            if isinstance(content, str):
                text = content
            else:
                text = " ".join(getattr(b, "text", "") or "" for b in content)
            if len(text) > content_max:
                text = text[:head] + "\n...[truncated]...\n" + text[-tail:]
            parts.append(f"[USER]: {text}")
            continue
        if isinstance(m, AssistantMessage):
            text_parts: list[str] = []
            tool_lines: list[str] = []
            for b in m.content:
                if isinstance(b, TextContent):
                    text_parts.append(b.text)
                elif isinstance(b, ToolUseBlock):
                    args_json = json.dumps(b.input or {}, ensure_ascii=False)
                    args_json = _truncate_tool_call_args_json(args_json, max_chars=1500)
                    tool_lines.append(f"  {b.name}({args_json})")
            text = "\n".join(text_parts)
            if len(text) > content_max:
                text = text[:head] + "\n...[truncated]...\n" + text[-tail:]
            if tool_lines:
                text = (
                    (text + "\n" if text else "") + "[Tool calls:\n" + "\n".join(tool_lines) + "\n]"
                )
            parts.append(f"[ASSISTANT]: {text}")
            continue
        if isinstance(m, ToolResultMessage):
            for r in m.results:
                body = _tool_result_body(r)
                if len(body) > content_max:
                    body = body[:head] + "\n...[truncated]...\n" + body[-tail:]
                parts.append(f"[TOOL RESULT {r.tool_use_id}]: {body}")
            continue
    return "\n\n".join(parts)


async def llm_structured_summarizer(
    messages: list[AgentMessage],
    *,
    summarizer_llm: Callable[[str], Awaitable[str]],
    prior_summary: str | None = None,
    insights_prefix: str | None = None,
) -> str:
    """LLM-based structured summariser with iterative updates.

    Builds the 12-section template and asks `summarizer_llm(prompt)` for
    the body. When `prior_summary` is provided, asks the LLM to fold new
    turns into it instead of starting fresh. Returns the prefixed body
    so callers can splice it directly as a SystemMessage.
    """
    serialized = _serialize_messages_for_summary(messages)
    insights_block = ""
    if insights_prefix:
        insights_block = (
            "\nPROVIDER-EXTRACTED INSIGHTS (preserve verbatim in the summary):\n"
            f"{insights_prefix}\n"
        )

    if prior_summary:
        prompt = (
            f"{_SUMMARIZER_PREAMBLE}\n\n"
            "You are updating a context compaction summary. A previous "
            "compaction produced the summary below. New conversation turns "
            "have occurred since then and need to be incorporated.\n\n"
            f"PREVIOUS SUMMARY:\n{prior_summary}\n\n"
            f"NEW TURNS TO INCORPORATE:\n{serialized}\n"
            f"{insights_block}\n"
            "Update the summary using this exact structure. PRESERVE all "
            "existing information that is still relevant. ADD new completed "
            "actions to the numbered list. Move items from 'In Progress' to "
            "'Completed Actions' when done. Move answered questions to "
            "'Resolved Questions'. CRITICAL: Update '## Active Task' to "
            "reflect the user's most recent unfulfilled request.\n\n"
            f"{_TEMPLATE_SECTIONS}"
        )
    else:
        prompt = (
            f"{_SUMMARIZER_PREAMBLE}\n\n"
            "Create a structured handoff summary for a different assistant "
            "that will continue this conversation after earlier turns are "
            "compacted.\n\n"
            f"TURNS TO SUMMARIZE:\n{serialized}\n"
            f"{insights_block}\n"
            "Use this exact structure:\n\n"
            f"{_TEMPLATE_SECTIONS}"
        )

    body = await summarizer_llm(prompt)
    body = (body or "").strip()
    if not body:
        return ""
    return f"{SUMMARY_PREFIX}\n{body}"


# ─── Pipeline orchestrator ───────────────────────────────────────────


async def structured_summarizer_pipeline(
    messages: list[AgentMessage],
    *,
    summarizer_llm: Callable[[str], Awaitable[str]],
    prior_summary: str | None = None,
    guard: CompactionGuard | None = None,
    keep_tail_turns: int = 6,
    insights_prefix: str | None = None,
) -> tuple[list[AgentMessage], str | None]:
    """Run the full structured-compaction pipeline.

    Steps:
      1. dedup tool_results by md5
      2. tool_result one-line pruning + tool-call args JSON truncation
         outside the protected tail
      3. align cut_idx backward to avoid splitting tool groups
      4. ensure_last_user_message_in_tail
      5. call ``llm_structured_summarizer`` on the compressed prefix
      6. orphan repair via ``_sanitize_tool_pairs``
      7. record before/after token counts in the guard

    Returns ``(new_messages, new_prior_summary)``. The pipeline is a
    no-op (returns the same list, same prior_summary) when the guard
    says to skip.
    """
    if guard is not None and guard.should_skip():
        logger.warning(
            "structured_summarizer_pipeline: skipping (last 2 compactions saved <%.0f%%)",
            guard.threshold_ratio * 100,
        )
        return list(messages), prior_summary

    if not messages:
        return [], prior_summary

    tokens_before = estimate_tokens(messages)
    working: list[AgentMessage] = list(messages)

    deduped = _dedup_tool_results_by_md5(working)
    if deduped:
        logger.info("structured pipeline: deduped %d tool_result(s)", deduped)

    n = len(working)
    cut_idx = max(1, n - keep_tail_turns)
    cut_idx = _align_boundary_backward(working, cut_idx)
    cut_idx = _ensure_last_user_message_in_tail(working, cut_idx)
    cut_idx = _align_boundary_backward(working, cut_idx)

    if cut_idx <= 0:
        if guard is not None:
            guard.record(tokens_before, tokens_before)
        return working, prior_summary

    # Tool-result one-line pruning + tool-call args JSON truncation
    # (in-place on the prefix; the tail stays untouched).
    for i in range(cut_idx):
        m = working[i]
        if isinstance(m, ToolResultMessage):
            for r in m.results:
                body = _tool_result_body(r)
                if len(body) > 200:
                    summary = _summarize_tool_result("unknown", body)
                    _set_tool_result_content(r, summary)
        elif isinstance(m, AssistantMessage):
            new_content: list[Any] = []
            modified = False
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    args_json = json.dumps(b.input or {}, ensure_ascii=False)
                    if len(args_json) > 4000:
                        truncated = _truncate_tool_call_args_json(args_json, max_chars=4000)
                        try:
                            new_args = json.loads(truncated)
                        except (ValueError, TypeError):
                            new_args = {"_truncated": True}
                        new_content.append(ToolUseBlock(id=b.id, name=b.name, input=new_args))
                        modified = True
                        continue
                new_content.append(b)
            if modified:
                try:
                    m.content = new_content  # type: ignore[misc]
                except Exception:
                    try:
                        object.__setattr__(m, "content", new_content)
                    except Exception:
                        pass

    to_summarize = working[:cut_idx]
    summary_text = await llm_structured_summarizer(
        to_summarize,
        summarizer_llm=summarizer_llm,
        prior_summary=prior_summary,
        insights_prefix=insights_prefix,
    )
    new_prior = summary_text or prior_summary

    if summary_text:
        summary_msg = SystemMessage(content=summary_text)
        new_messages: list[AgentMessage] = [summary_msg, *working[cut_idx:]]
    else:
        new_messages = list(working[cut_idx:])

    repaired = _sanitize_tool_pairs(new_messages)
    if repaired:
        logger.info("structured pipeline: repaired %d orphan tool pair(s)", repaired)

    tokens_after = estimate_tokens(new_messages)
    if guard is not None:
        guard.record(tokens_before, tokens_after)
    logger.info(
        "structured pipeline: %d -> %d tokens (saved %d, kept %d tail msgs)",
        tokens_before,
        tokens_after,
        tokens_before - tokens_after,
        len(new_messages) - (1 if summary_text else 0),
    )
    return new_messages, new_prior


__all__ = [
    "SUMMARY_PREFIX",
    "CompactReason",
    "CompactionGuard",
    "CompactionPlan",
    "SummarizerFn",
    "apply_compaction",
    "decide_compaction",
    "llm_structured_summarizer",
    "maybe_compact",
    "should_skip_compaction",
    "structured_summarizer_pipeline",
    "truncating_summarizer",
]
