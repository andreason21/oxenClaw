"""Context-window pre-flight guard.

Mirrors openclaw `agents/context-window-guard.ts:1-74`. Reject runs
against models with effectively-unusable context windows up front so
the run loop doesn't silently spiral into preemptive compaction at
iteration 1 — we want a loud, structural error instead.

Two thresholds:
  - `CONTEXT_WINDOW_HARD_MIN` (16k): below this the model is unusable
    for oxenclaw's typical workload (memory + skill manifests + tools).
  - `CONTEXT_WINDOW_WARN_BELOW` (32k): below this we emit a warning so
    operators see the cliff coming before they hit it.

The guard is intentionally cheap and stateless — call it from PiAgent
init (or any caller wanting the same gate) with `model.context_window`.
"""

from __future__ import annotations

from dataclasses import dataclass

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.context_window_guard")

CONTEXT_WINDOW_HARD_MIN_TOKENS = 16_000
CONTEXT_WINDOW_WARN_BELOW_TOKENS = 32_000


@dataclass(frozen=True)
class ContextWindowGuardResult:
    tokens: int
    should_warn: bool
    should_block: bool


def evaluate_context_window_guard(
    tokens: int | None,
    *,
    warn_below: int = CONTEXT_WINDOW_WARN_BELOW_TOKENS,
    hard_min: int = CONTEXT_WINDOW_HARD_MIN_TOKENS,
) -> ContextWindowGuardResult:
    """Return the (warn, block) verdict for a given context window size.

    `tokens=None` or non-positive disables the check (returns no-warn/no-block)
    so a caller without a known window doesn't get a spurious block. Operators
    typically configure context_window correctly; the guard targets the
    misconfigured-tiny case (e.g. a coding agent pinned to a 4k chat model).
    """
    n = max(0, int(tokens or 0))
    if n <= 0:
        return ContextWindowGuardResult(tokens=0, should_warn=False, should_block=False)
    warn_below = max(1, int(warn_below))
    hard_min = max(1, int(hard_min))
    return ContextWindowGuardResult(
        tokens=n,
        should_warn=n < warn_below,
        should_block=n < hard_min,
    )


class ContextWindowTooSmallError(RuntimeError):
    """Raised when a model's context window falls below the hard minimum."""


def assert_context_window_usable(
    model_id: str,
    tokens: int | None,
    *,
    warn_below: int = CONTEXT_WINDOW_WARN_BELOW_TOKENS,
    hard_min: int = CONTEXT_WINDOW_HARD_MIN_TOKENS,
) -> ContextWindowGuardResult:
    """Convenience for callers that want loud failure on under-spec models.

    Logs a warning when below `warn_below`, raises when below `hard_min`.
    Returns the verdict so the caller can also surface it in telemetry.
    """
    verdict = evaluate_context_window_guard(tokens, warn_below=warn_below, hard_min=hard_min)
    if verdict.should_block:
        raise ContextWindowTooSmallError(
            f"model {model_id!r} context_window={verdict.tokens} is below the hard "
            f"minimum {hard_min}; oxenclaw cannot run safely on this model — "
            "configure a model with a larger window or override hard_min explicitly."
        )
    if verdict.should_warn:
        logger.warning(
            "context window below recommended floor: model=%s tokens=%d (warn_below=%d)",
            model_id,
            verdict.tokens,
            warn_below,
        )
    return verdict


__all__ = [
    "CONTEXT_WINDOW_HARD_MIN_TOKENS",
    "CONTEXT_WINDOW_WARN_BELOW_TOKENS",
    "ContextWindowGuardResult",
    "ContextWindowTooSmallError",
    "assert_context_window_usable",
    "evaluate_context_window_guard",
]
