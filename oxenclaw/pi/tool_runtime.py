"""Tool runtime hardening.

Mirrors `pi-embedded-runner/tool-result-truncation.ts` +
`tool-result-context-guard.ts` + `tool-result-char-estimator.ts` +
`tool-name-allowlist.ts` + `effective-tool-policy.ts` +
`tool-schema-runtime.ts` + `tool-split.ts`.

Pieces:
- `truncate_tool_result(text, *, max_chars)` — character-budget tail clip
  with a [...truncated N chars] sentinel.
- `estimate_tool_result_chars(payload)` — fast char count over arbitrary
  JSON-serialisable payloads.
- `apply_context_guard(...)` — when the running input-token estimate
  passes a threshold, aggressively reduce the size of *future* tool
  results by lowering `max_chars` for the next call.
- `ToolNameAllowlist` — pattern-based allow/deny list (glob style).
- `EffectiveToolPolicy.resolve(tools)` — apply allowlist + per-name
  overrides + global defaults to produce the actual tool list a turn
  will see.
- `split_large_tool(tool, items)` — utility for tools that produce huge
  arrays (e.g. file listings); returns chunks the model can request
  one at a time.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from oxenclaw.pi.tools import AgentTool

DEFAULT_MAX_TOOL_RESULT_CHARS = 32_000  # ~8K tokens; sane default
MIN_TOOL_RESULT_CHARS = 1_024  # never go lower than this even under pressure


# ─── result truncation + estimation ─────────────────────────────────


def estimate_tool_result_chars(payload: Any) -> int:
    """Char count for a JSON-serialisable tool result.

    Avoids tokenizer cost; ~3.5 chars/token is the runtime's working ratio.
    """
    if isinstance(payload, str):
        return len(payload)
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(payload))


def truncate_tool_result(text: str, *, max_chars: int) -> tuple[str, bool]:
    """Truncate to `max_chars`, keeping the head + a sentinel tail count.

    Returns `(possibly_truncated_text, was_truncated)`.
    """
    if max_chars <= 0:
        return "", True
    if len(text) <= max_chars:
        return text, False
    head_keep = max(0, max_chars - 64)
    dropped = len(text) - head_keep
    sentinel = f"\n\n[...truncated {dropped} chars]"
    return text[:head_keep] + sentinel, True


# ─── context guard ──────────────────────────────────────────────────


@dataclass
class ToolContextGuardState:
    """Per-session state used by `apply_context_guard`."""

    consecutive_pressure_turns: int = 0
    current_max_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS


def apply_context_guard(
    state: ToolContextGuardState,
    *,
    used_tokens: int,
    model_context_tokens: int,
    pressure_ratio: float = 0.7,
    relief_ratio: float = 0.5,
) -> int:
    """Adjust `state.current_max_chars` based on context pressure.

    - When usage > pressure_ratio * window: halve the budget (down to MIN).
    - When usage < relief_ratio * window for two turns: grow back toward
      DEFAULT.
    Returns the new char budget for the next tool call.
    """
    ratio = used_tokens / max(1, model_context_tokens)
    if ratio > pressure_ratio:
        state.consecutive_pressure_turns += 1
        new_budget = max(MIN_TOOL_RESULT_CHARS, state.current_max_chars // 2)
        state.current_max_chars = new_budget
    elif ratio < relief_ratio:
        state.consecutive_pressure_turns = 0
        # Slowly grow back to default.
        new_budget = min(DEFAULT_MAX_TOOL_RESULT_CHARS, state.current_max_chars * 2)
        state.current_max_chars = new_budget
    return state.current_max_chars


# ─── name allowlist ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolNameAllowlist:
    """Glob-style allow/deny lists. Empty allow == allow all (subject to deny)."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def is_allowed(self, name: str) -> bool:
        for pat in self.deny:
            if fnmatch.fnmatchcase(name, pat):
                return False
        if not self.allow:
            return True
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.allow)

    def filter(self, tools: Iterable[AgentTool]) -> list[AgentTool]:
        return [t for t in tools if self.is_allowed(t.name)]


# ─── effective tool policy ──────────────────────────────────────────


@dataclass(frozen=True)
class ToolOverride:
    """Per-tool policy override."""

    name: str
    enabled: bool = True
    max_result_chars: int | None = None


@dataclass(frozen=True)
class EffectiveToolPolicy:
    """Combine global defaults + allowlist + per-tool overrides."""

    allowlist: ToolNameAllowlist = ToolNameAllowlist()
    overrides: tuple[ToolOverride, ...] = ()
    default_max_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS

    def _override_for(self, name: str) -> ToolOverride | None:
        for o in self.overrides:
            if o.name == name:
                return o
        return None

    def resolve(self, tools: Iterable[AgentTool]) -> list[AgentTool]:
        """Apply allowlist + override.enabled flag → final tool list."""
        out: list[AgentTool] = []
        for t in tools:
            if not self.allowlist.is_allowed(t.name):
                continue
            ov = self._override_for(t.name)
            if ov is not None and not ov.enabled:
                continue
            out.append(t)
        return out

    def max_chars_for(self, name: str) -> int:
        ov = self._override_for(name)
        if ov is not None and ov.max_result_chars is not None:
            return ov.max_result_chars
        return self.default_max_result_chars


# ─── tool result splitter (paginated outputs) ───────────────────────


@dataclass(frozen=True)
class ToolPage:
    """One page of a paginated tool output."""

    page: int
    total_pages: int
    content: str
    has_more: bool


def split_large_payload(
    items: list[Any],
    *,
    page_chars: int = 8_000,
    serializer: Literal["json", "lines"] = "lines",
) -> list[ToolPage]:
    """Paginate a long item list into chunks the model can request one at a
    time. The runtime hands back page 1; subsequent pages come from a
    follow-up tool call referencing a `page_token` (out of scope here —
    the helper just produces the chunks)."""
    if not items:
        return [ToolPage(page=1, total_pages=1, content="", has_more=False)]

    def _format(item: Any) -> str:
        if serializer == "json":
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, str):
            return item
        return json.dumps(item, ensure_ascii=False)

    pages: list[list[str]] = [[]]
    cur_size = 0
    for item in items:
        s = _format(item)
        if cur_size + len(s) + 1 > page_chars and pages[-1]:
            pages.append([])
            cur_size = 0
        pages[-1].append(s)
        cur_size += len(s) + 1
    total = len(pages)
    out: list[ToolPage] = []
    for i, chunk in enumerate(pages, start=1):
        sep = "\n" if serializer == "lines" else ","
        body = sep.join(chunk)
        if serializer == "json":
            body = "[" + body + "]"
        out.append(
            ToolPage(
                page=i,
                total_pages=total,
                content=body,
                has_more=i < total,
            )
        )
    return out


__all__ = [
    "DEFAULT_MAX_TOOL_RESULT_CHARS",
    "MIN_TOOL_RESULT_CHARS",
    "EffectiveToolPolicy",
    "ToolContextGuardState",
    "ToolNameAllowlist",
    "ToolOverride",
    "ToolPage",
    "apply_context_guard",
    "estimate_tool_result_chars",
    "split_large_payload",
    "truncate_tool_result",
]
