"""Per-process rate-limit header tracker.

Captures `anthropic-ratelimit-*` and OpenAI-style `x-ratelimit-*`
headers from provider responses so subsequent requests can short-
circuit before they hit a 429.  The classifier in
`oxenclaw/pi/run/error_classifier.py` consults
`is_quota_exhausted(state)` to decide whether to rotate the
credential pre-emptively.

In-memory only (process-local) — no on-disk persistence.  The tracker
is optional everywhere it's wired in: callers pass `None` when they
don't need it, and the rest of the run loop behaves exactly as before.

Header schema we understand:
    anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}
    x-ratelimit-{limit,remaining,reset}-{requests,tokens}
    x-ratelimit-{limit,remaining,reset}-{requests,tokens}-1h

Reset values may be either:
    - epoch-seconds (anything > 1e9 is treated as absolute), or
    - relative-seconds (added to `time.time()` at parse).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitState:
    """Snapshot of one provider/key's rate-limit budget at a point in time."""

    requests_remaining: int | None = None
    requests_reset_at: float | None = None  # epoch seconds
    tokens_remaining: int | None = None
    tokens_reset_at: float | None = None  # epoch seconds
    requests_limit: int | None = None
    tokens_limit: int | None = None
    observed_at: float = 0.0


_EPOCH_THRESHOLD = 1_000_000_000.0  # > Sat 9 Sep 2001 → absolute epoch


def _coerce_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_reset(value: str | None, *, now: float) -> float | None:
    """Reset values may be epoch-seconds or relative-seconds.

    Provider hints `> 1e9` are absolute epoch; smaller values are
    seconds-from-now.  Always returns absolute epoch (or None).
    """
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return now
    if n > _EPOCH_THRESHOLD:
        return n
    return now + n


def parse_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitState | None:
    """Parse rate-limit headers from a provider response.

    Returns None when no relevant headers are present.  Otherwise
    returns the most permissive (largest remaining, earliest reset)
    state across the per-minute and per-hour windows the provider
    sent.
    """
    if not headers:
        return None
    # Header names are case-insensitive (RFC 7230) — normalise.
    lowered = {k.lower(): v for k, v in headers.items()}

    # Quick reject: nothing here looks like a rate-limit header.
    if not any(
        k.startswith("x-ratelimit-") or k.startswith("anthropic-ratelimit-") for k in lowered
    ):
        return None

    now = time.time()

    # Anthropic schema: anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}
    a_req_limit = _coerce_int(lowered.get("anthropic-ratelimit-requests-limit"))
    a_req_remaining = _coerce_int(lowered.get("anthropic-ratelimit-requests-remaining"))
    a_req_reset = _coerce_reset(lowered.get("anthropic-ratelimit-requests-reset"), now=now)
    a_tok_limit = _coerce_int(lowered.get("anthropic-ratelimit-tokens-limit"))
    a_tok_remaining = _coerce_int(lowered.get("anthropic-ratelimit-tokens-remaining"))
    a_tok_reset = _coerce_reset(lowered.get("anthropic-ratelimit-tokens-reset"), now=now)

    # OpenAI schema: x-ratelimit-{limit,remaining,reset}-{requests,tokens}[-1h]
    # We pick the most-restrictive (smallest remaining) across the 1m/1h windows.
    def _pick_remaining(*keys: str) -> int | None:
        vals = [_coerce_int(lowered.get(k)) for k in keys]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def _pick_limit(*keys: str) -> int | None:
        vals = [_coerce_int(lowered.get(k)) for k in keys]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def _pick_reset(*keys: str) -> float | None:
        vals = [_coerce_reset(lowered.get(k), now=now) for k in keys]
        vals = [v for v in vals if v is not None]
        # The earliest reset (smallest epoch) is what matters for "when can
        # I retry"; that's the window the provider will check next.
        return min(vals) if vals else None

    o_req_limit = _pick_limit("x-ratelimit-limit-requests", "x-ratelimit-limit-requests-1h")
    o_req_remaining = _pick_remaining(
        "x-ratelimit-remaining-requests", "x-ratelimit-remaining-requests-1h"
    )
    o_req_reset = _pick_reset("x-ratelimit-reset-requests", "x-ratelimit-reset-requests-1h")
    o_tok_limit = _pick_limit("x-ratelimit-limit-tokens", "x-ratelimit-limit-tokens-1h")
    o_tok_remaining = _pick_remaining(
        "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-tokens-1h"
    )
    o_tok_reset = _pick_reset("x-ratelimit-reset-tokens", "x-ratelimit-reset-tokens-1h")

    # Merge: prefer the smallest remaining / earliest reset across providers
    # (so we err on the side of treating the key as exhausted earlier).
    def _merge_remaining(a: int | None, b: int | None) -> int | None:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)

    def _merge_reset(a: float | None, b: float | None) -> float | None:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)

    def _merge_limit(a: int | None, b: int | None) -> int | None:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)

    state = RateLimitState(
        requests_remaining=_merge_remaining(a_req_remaining, o_req_remaining),
        requests_reset_at=_merge_reset(a_req_reset, o_req_reset),
        tokens_remaining=_merge_remaining(a_tok_remaining, o_tok_remaining),
        tokens_reset_at=_merge_reset(a_tok_reset, o_tok_reset),
        requests_limit=_merge_limit(a_req_limit, o_req_limit),
        tokens_limit=_merge_limit(a_tok_limit, o_tok_limit),
        observed_at=now,
    )

    # If literally nothing parsed, return None so callers can short-circuit.
    if (
        state.requests_remaining is None
        and state.tokens_remaining is None
        and state.requests_reset_at is None
        and state.tokens_reset_at is None
        and state.requests_limit is None
        and state.tokens_limit is None
    ):
        return None
    return state


class RateLimitTracker:
    """Process-local thread-safe tracker keyed by `(provider, key_id)`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], RateLimitState] = {}

    def record(self, provider: str, key_id: str, state: RateLimitState) -> None:
        """Store the latest observed state for this provider/key."""
        with self._lock:
            self._state[(provider, key_id)] = state

    def peek(self, provider: str, key_id: str) -> RateLimitState | None:
        """Return the most recent state for this provider/key, or None."""
        with self._lock:
            return self._state.get((provider, key_id))

    def clear(self, provider: str | None = None) -> None:
        """Drop tracked state — useful between tests."""
        with self._lock:
            if provider is None:
                self._state.clear()
            else:
                for key in list(self._state.keys()):
                    if key[0] == provider:
                        del self._state[key]


def is_quota_exhausted(state: RateLimitState | None) -> bool:
    """True when the key is effectively dead for the next ≥ 60s.

    Used by the error classifier to decide whether to rotate the
    credential pool aggressively rather than wait it out.  We require
    BOTH `remaining <= 0` AND `reset_at - now >= 60s` so a brief
    resets-in-2s lull doesn't trigger pool rotation.
    """
    if state is None:
        return False
    now = time.time()
    # Requests bucket exhausted?
    req_dead = (
        state.requests_remaining is not None
        and state.requests_remaining <= 0
        and state.requests_reset_at is not None
        and (state.requests_reset_at - now) >= 60.0
    )
    # Tokens bucket exhausted?
    tok_dead = (
        state.tokens_remaining is not None
        and state.tokens_remaining <= 0
        and state.tokens_reset_at is not None
        and (state.tokens_reset_at - now) >= 60.0
    )
    return req_dead or tok_dead


__all__ = [
    "RateLimitState",
    "RateLimitTracker",
    "is_quota_exhausted",
    "parse_rate_limit_headers",
]
