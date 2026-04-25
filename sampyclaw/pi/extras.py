"""Phase 9 extras — small modules that the run loop composes.

Mirrors the leftover utility files in `pi-embedded-runner/`:
- `result-fallback-classifier.ts` → `classify_failure`
- `assistant-failover.ts`          → `select_failover_model`
- `lanes.ts`                       → `LaneRouter`
- `usage-accumulator.ts`           → `UsageAccumulator`
- `usage-reporting.ts`             → `summarize_usage`
- `transcript-rewrite.ts`          → `rewrite_transcript`
- `extra-params.ts`                → `merge_extra_params`
- `wait-for-idle-before-flush.ts`  → `wait_for_idle`
- `abort.ts`                       → `cancel_on`
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sampyclaw.pi.messages import AgentMessage, AssistantMessage, TextContent
from sampyclaw.pi.models import Model


# ─── failure classifier ─────────────────────────────────────────────


FailureCategory = Literal[
    "transient", "rate_limit", "auth", "context_overflow", "model_error",
    "client_error", "unknown",
]


_RATE_RE = re.compile(r"\b(rate|quota|429)\b", re.IGNORECASE)
_AUTH_RE = re.compile(r"\b(401|403|invalid api key|unauthor)\b", re.IGNORECASE)
_OVERFLOW_RE = re.compile(
    r"\b(context length|token limit|too many tokens|max context)\b",
    re.IGNORECASE,
)
_TRANSIENT_RE = re.compile(
    r"\b(timed out|timeout|reset|temporarily unavailable|503|504|529)\b",
    re.IGNORECASE,
)
_CLIENT_RE = re.compile(r"\b400\b|invalid request|bad request", re.IGNORECASE)


def classify_failure(message: str) -> FailureCategory:
    """Map an error message to a category the run loop can decide on.

    Categories drive the loop's response: `transient`/`rate_limit` →
    backoff+retry; `context_overflow` → trigger compaction; `auth` → fail
    fast; `model_error` → consider failover.
    """
    if not message:
        return "unknown"
    if _RATE_RE.search(message):
        return "rate_limit"
    if _AUTH_RE.search(message):
        return "auth"
    if _OVERFLOW_RE.search(message):
        return "context_overflow"
    if _TRANSIENT_RE.search(message):
        return "transient"
    if _CLIENT_RE.search(message):
        return "client_error"
    return "unknown"


# ─── failover ───────────────────────────────────────────────────────


def select_failover_model(
    primary: Model, candidates: Iterable[Model]
) -> Model | None:
    """Pick the next-best model when `primary` fails.

    Strategy: prefer same provider with similar context window, then any
    other model whose context window is ≥ 80% of primary's. Ignore the
    primary itself. Returns None if nothing suitable.
    """
    pool = [m for m in candidates if m.id != primary.id]
    if not pool:
        return None
    same_provider = [m for m in pool if m.provider == primary.provider]
    if same_provider:
        same_provider.sort(
            key=lambda m: abs(m.context_window - primary.context_window)
        )
        return same_provider[0]
    floor = int(primary.context_window * 0.8)
    big_enough = [m for m in pool if m.context_window >= floor]
    if not big_enough:
        return None
    big_enough.sort(key=lambda m: -m.context_window)
    return big_enough[0]


# ─── lane router ────────────────────────────────────────────────────


@dataclass
class LaneRouter:
    """Concurrency limiter per "lane" (e.g. provider, agent, session).

    The run loop calls `acquire(lane_id)` before issuing a provider call
    and `release(lane_id)` after. Lanes that have no explicit cap fall
    back to `default_cap`.
    """

    default_cap: int = 4
    caps: dict[str, int] = field(default_factory=dict)
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def _semaphore_for(self, lane: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(lane)
        if sem is None:
            sem = asyncio.Semaphore(self.caps.get(lane, self.default_cap))
            self._semaphores[lane] = sem
        return sem

    async def acquire(self, lane: str) -> None:
        await self._semaphore_for(lane).acquire()

    def release(self, lane: str) -> None:
        sem = self._semaphores.get(lane)
        if sem is not None:
            sem.release()


# ─── usage accumulator ──────────────────────────────────────────────


@dataclass
class UsageAccumulator:
    """Per-key token + cost totals across a session."""

    totals: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0

    def add(self, usage: dict[str, Any] | None, *, pricing: dict[str, float] | None = None) -> None:
        if not usage:
            return
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                self.totals[key] = int(self.totals.get(key, 0) + value)
        if pricing:
            for key, per_million in pricing.items():
                tokens = self.totals.get(key, 0)
                if tokens > 0:
                    self.cost_usd = self.cost_usd + (tokens / 1_000_000) * per_million

    def summarize(self) -> dict[str, Any]:
        return {**self.totals, "cost_usd": round(self.cost_usd, 6)}


def summarize_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of per-turn usage dicts."""
    acc = UsageAccumulator()
    for u in usages:
        acc.add(u)
    return acc.summarize()


# ─── transcript rewrite ─────────────────────────────────────────────


def rewrite_transcript(
    messages: list[AgentMessage],
    *,
    redact_tokens: tuple[str, ...] = (),
    drop_thinking: bool = False,
) -> list[AgentMessage]:
    """Return a copy with secret-token redaction + optional thinking removal.

    Used at session-export time so transcripts can be safely shared. Does
    NOT mutate the input.
    """
    from sampyclaw.pi.messages import ThinkingBlock, ToolUseBlock

    out: list[AgentMessage] = []
    for msg in messages:
        dumped = msg.model_dump()
        if dumped.get("role") == "assistant":
            content = []
            for block in dumped.get("content", []):
                if drop_thinking and block.get("type") == "thinking":
                    continue
                content.append(_redact_block(block, redact_tokens))
            dumped["content"] = content
        elif dumped.get("role") == "user" and isinstance(
            dumped.get("content"), str
        ):
            dumped["content"] = _redact_str(dumped["content"], redact_tokens)
        from pydantic import TypeAdapter

        out.append(TypeAdapter(AgentMessage).validate_python(dumped))
    return out


def _redact_str(s: str, tokens: tuple[str, ...]) -> str:
    if not tokens:
        return s
    out = s
    for tok in tokens:
        if tok and tok in out:
            out = out.replace(tok, "[REDACTED]")
    return out


def _redact_block(block: dict[str, Any], tokens: tuple[str, ...]) -> dict[str, Any]:
    if not tokens:
        return block
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        block["text"] = _redact_str(block["text"], tokens)
    return block


# ─── extra params merge ─────────────────────────────────────────────


def merge_extra_params(
    *layers: dict[str, Any] | None,
) -> dict[str, Any]:
    """Right-most wins. Drops None layers. Used to combine model defaults +
    runtime overrides + per-attempt extras."""
    out: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        out.update(layer)
    return out


# ─── wait for idle / abort helpers ──────────────────────────────────


async def wait_for_idle(idle_for: float = 0.05) -> None:
    """Yield until the event loop has been idle for `idle_for` seconds.

    The pi runtime uses this to flush pending event-emit tasks before
    closing a stream so subscribers see the last delta."""
    loop = asyncio.get_running_loop()
    last = loop.time()
    # One short sleep is sufficient as an idle proxy in single-task tests;
    # the original TS implementation polls the microtask queue. asyncio's
    # `sleep(0)` already drains pending callbacks once.
    await asyncio.sleep(0)
    if loop.time() - last < idle_for:
        await asyncio.sleep(idle_for)


def cancel_on(event: asyncio.Event) -> asyncio.Future[None]:
    """Return a future that resolves when `event` is set. Useful for
    `asyncio.wait([task, cancel_on(abort)], FIRST_COMPLETED)`."""
    fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def _on_set() -> None:
        if not fut.done():
            fut.set_result(None)

    if event.is_set():
        _on_set()
        return fut

    async def _waiter() -> None:
        await event.wait()
        _on_set()

    asyncio.create_task(_waiter())
    return fut


__all__ = [
    "FailureCategory",
    "LaneRouter",
    "UsageAccumulator",
    "cancel_on",
    "classify_failure",
    "merge_extra_params",
    "rewrite_transcript",
    "select_failover_model",
    "summarize_usage",
    "wait_for_idle",
]
