"""Assistant-failover — switch to a backup model when the primary fails.

Mirrors openclaw `pi-embedded-runner/run/assistant-failover.ts`. The
loop calls `should_failover` after a failed `run_attempt`. If yes,
it picks the next model from `RuntimeConfig.failover_chain`, swaps
the model object, and retries the SAME turn.

Failure conditions:
  - HTTP 5xx or "model unavailable" error code from the provider
  - Stream stalls past `llm_idle_timeout_seconds` (already detected
    upstream and surfaced as ErrorEvent)
  - `recovery_streak` consecutive empty replies after stop-recovery
    has exhausted its budget
  - operator-defined predicate via `failover_predicate` callable

Once failover fires, the new model is sticky for the remainder of the
turn — we don't bounce back unless the backup ALSO fails (then move
to the next entry in the chain).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from oxenclaw.pi.run.attempt import AttemptResult
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.failover")


# Stop reasons / error markers that count as "model is the problem".
_FAILOVER_STOP_REASONS: frozenset[str] = frozenset(
    {
        "error",
        "model_error",
        "provider_error",
        "overloaded",
        "rate_limit",
    }
)


@dataclass
class FailoverDecision:
    failover: bool
    reason: str = ""
    next_model_id: str | None = None


def should_failover(
    *,
    result: AttemptResult,
    chain: list[str],
    chain_cursor: int,
    empty_streak: int,
    empty_streak_threshold: int = 3,
    custom_predicate: Callable[[AttemptResult], bool] | None = None,
) -> FailoverDecision:
    """Decide whether to swap to the next model in `chain`."""
    if chain_cursor + 1 >= len(chain):
        return FailoverDecision(failover=False, reason="end_of_chain")
    next_id = chain[chain_cursor + 1]

    # Custom hook always wins.
    if custom_predicate is not None and custom_predicate(result):
        return FailoverDecision(failover=True, reason="custom_predicate", next_model_id=next_id)

    # Hard error from the provider.
    err = result.error
    if err is not None:
        if err.retryable is False:
            return FailoverDecision(
                failover=True,
                reason=f"non_retryable:{err.message[:60]}",
                next_model_id=next_id,
            )
        # Retryable provider errors get N retries upstream; if we're
        # invoked AFTER those, the error is structural and failover
        # is the right move.
        return FailoverDecision(
            failover=True,
            reason=f"provider_error:{err.message[:60]}",
            next_model_id=next_id,
        )
    # Stop-reason-based.
    stop = result.message.stop_reason
    if stop in _FAILOVER_STOP_REASONS:
        return FailoverDecision(failover=True, reason=f"stop_reason:{stop}", next_model_id=next_id)
    # Repeated empty replies after stop-recovery exhausted.
    if empty_streak >= empty_streak_threshold:
        return FailoverDecision(
            failover=True,
            reason=f"empty_streak:{empty_streak}>={empty_streak_threshold}",
            next_model_id=next_id,
        )
    return FailoverDecision(failover=False, reason="model_ok")


def resolve_next_model(chain: list[str], cursor: int, registry: Any) -> tuple[Any | None, int]:
    """Walk the chain forward from `cursor` until we find a registered
    model. Returns `(model_or_None, new_cursor)`. Skips entries that
    aren't in the registry (operator typo / removed alias)."""
    n = len(chain)
    for offset in range(1, n):
        idx = cursor + offset
        if idx >= n:
            return None, n
        model_id = chain[idx]
        try:
            model = registry.require(model_id)
            return model, idx
        except KeyError:
            logger.warning(
                "failover: model %r in chain not registered — skipping",
                model_id,
            )
            continue
    return None, n


__all__ = ["FailoverDecision", "resolve_next_model", "should_failover"]
