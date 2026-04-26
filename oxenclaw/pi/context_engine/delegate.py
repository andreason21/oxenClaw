"""Bridge from a `ContextEngine.compact()` call to the built-in pi compaction.

Mirrors openclaw `src/context-engine/delegate.ts:delegateCompactionToRuntime`.
Engines that don't own the compaction algorithm but still need overflow
recovery to use the stock runtime path call this from their own
`compact()` implementation.
"""

from __future__ import annotations

from oxenclaw.pi.compaction import (
    apply_compaction,
    decide_compaction,
    truncating_summarizer,
)
from oxenclaw.pi.context_engine.types import CompactResult
from oxenclaw.pi.messages import AgentMessage

# Conservative default when the host doesn't pass a token budget.
_DEFAULT_TOKEN_BUDGET = 32_000
_DEFAULT_KEEP_TAIL_TURNS = 6
_DEFAULT_THRESHOLD_RATIO = 0.85


async def delegate_compaction_to_runtime(
    *,
    session_id: str,
    messages: list[AgentMessage],
    token_budget: int | None = None,
    current_token_count: int | None = None,
    force: bool = False,
    compaction_target: str = "threshold",
) -> CompactResult:
    """Run pi.compaction.{decide,apply} for an engine that wants the
    stock behaviour. Returns a `CompactResult` reporting what happened.

    `compaction_target="budget"` lowers the threshold ratio so the plan
    drops more aggressively (manual `/compact` UX); `"threshold"` keeps
    the default 0.85 high-water mark.
    """
    budget = token_budget or _DEFAULT_TOKEN_BUDGET
    threshold_ratio = 0.5 if compaction_target == "budget" else _DEFAULT_THRESHOLD_RATIO
    plan = decide_compaction(
        messages,
        model_context_tokens=budget,
        threshold_ratio=threshold_ratio,
        keep_tail_turns=_DEFAULT_KEEP_TAIL_TURNS,
        reason="manual" if force else "auto",
        force=force,
    )
    if not plan.needed:
        return CompactResult(
            ok=True,
            compacted=False,
            reason="below threshold" if not force else "no safe boundary",
            tokens_before=plan.tokens_before,
            tokens_after=plan.tokens_before,
        )
    _, entry = await apply_compaction(messages, plan, truncating_summarizer)
    return CompactResult(
        ok=True,
        compacted=True,
        reason=plan.reason,
        summary=entry.summary or None,
        first_kept_entry_id=None,
        tokens_before=entry.tokens_before,
        tokens_after=entry.tokens_after,
    )


__all__ = ["delegate_compaction_to_runtime"]
