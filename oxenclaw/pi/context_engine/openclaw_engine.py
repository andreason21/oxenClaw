"""OpenclawContextEngine — proactive trimming + active-memory ready.

oxenClaw original despite the name; "openclaw-style" describes the
*shape* of the behaviour (eager pre-call shaping rather than reactive
post-call) rather than a 1:1 port. Drop-in replacement for
`LegacyContextEngine` that adds:

  - `assemble()` checks the running token budget against the model
    window; when above `proactive_threshold_ratio` (default 0.80),
    older tool_result content is shrunk in-place before the messages
    leave the engine — so the model never sees a bloated transcript
    even when the per-turn `preemptive_compaction.decide()` happens to
    say "noop".
  - All other hooks defer to LegacyContextEngine, preserving the
    session-aware compact() path and the rest of the protocol surface.

Stateless: PiAgent owns persistence; the engine just shapes the
context window for the model call. Multiple sessions can share a
single instance.
"""

from __future__ import annotations

from oxenclaw.pi.context_engine.legacy import LegacyContextEngine
from oxenclaw.pi.context_engine.types import (
    AssembleResult,
    ContextEngineInfo,
)
from oxenclaw.pi.messages import AgentMessage, ToolResultBlock, ToolResultMessage
from oxenclaw.pi.run.preemptive_compaction import estimate_prompt_tokens
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.context_engine.openclaw")


class OpenclawContextEngine(LegacyContextEngine):
    """Proactive-trim engine. Mirrors openclaw's default context shaping."""

    info = ContextEngineInfo(
        id="openclaw",
        name="Openclaw proactive trim",
        version="1.0",
        owns_compaction=False,
        turn_maintenance_mode="foreground",
    )

    # When estimated/budget exceeds this ratio, proactively trim
    # tool_result bodies. 0.80 leaves headroom below the 0.85 hard
    # threshold the run loop's preemptive_compaction uses.
    proactive_threshold_ratio: float = 0.80
    # Per-tool_result keep budget when trimming kicks in.
    proactive_keep_chars: int = 1024

    async def assemble(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        token_budget: int | None = None,
        session_key: str | None = None,
        available_tools: set[str] | None = None,
    ) -> AssembleResult:
        out_messages: list[AgentMessage] = list(messages)
        # Use the run-loop's tool-result-aware estimator so ToolResultMessage
        # bodies are counted (the legacy `_message_text` returns "" for
        # them, missing the largest contributors to context bloat).
        total = estimate_prompt_tokens(system=None, messages=out_messages)
        if (
            token_budget
            and token_budget > 0
            and total > int(token_budget * self.proactive_threshold_ratio)
        ):
            removed = _trim_tool_results_inplace(out_messages, keep_chars=self.proactive_keep_chars)
            if removed:
                logger.info(
                    "openclaw context-engine proactive trim: removed=%d "
                    "estimated=%d budget=%d ratio=%.2f",
                    removed,
                    total,
                    token_budget,
                    total / token_budget,
                )
                # Re-estimate after trim so callers see the post-trim total.
                total = estimate_prompt_tokens(system=None, messages=out_messages)
        return AssembleResult(messages=out_messages, estimated_tokens=total)


def _trim_tool_results_inplace(messages: list[AgentMessage], *, keep_chars: int) -> int:
    """In-place tool_result body trim. Returns removed char count.

    Walks ToolResultMessage entries; any ToolResultBlock whose content
    exceeds `keep_chars` gets truncated with a `[...trimmed N chars]`
    sentinel. ToolResultBlock is a frozen dataclass so we use
    `object.__setattr__` (matches `preemptive_compaction.truncate_tool_results`).
    """
    removed = 0
    for m in messages:
        if not isinstance(m, ToolResultMessage):
            continue
        for r in m.results:
            if not isinstance(r, ToolResultBlock):
                continue
            content = r.content or ""
            if len(content) <= keep_chars:
                continue
            new_content = content[:keep_chars] + f"\n[...trimmed {len(content) - keep_chars} chars]"
            removed += len(content) - len(new_content)
            try:
                object.__setattr__(r, "content", new_content)
            except Exception:
                pass
    return removed


__all__ = ["OpenclawContextEngine"]
