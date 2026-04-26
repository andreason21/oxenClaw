"""Prompt-cache observability + retention.

Mirrors `pi-embedded-runner/prompt-cache-observability.ts` +
`prompt-cache-retention.ts` + `cache-ttl.ts`. Anthropic's prompt-cache
returns three counts in `usage`:
- `cache_read_input_tokens`  — what we got back from cache (the win)
- `cache_creation_input_tokens` — what we paid to write into cache
- `input_tokens` — non-cached read

A healthy steady-state should see `cache_read / (cache_read + input)`
approaching 1.0 once the first turn warms the cache. The observer in this
module accumulates per-session totals + per-turn deltas and exposes a
`hit_rate()` method. The retention helper decides whether to *keep* a
cache marker on a long-stale conversation; below the TTL the cache is
"alive" and we keep markers, otherwise we drop them to save cache_create
costs that won't be recouped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Anthropic's prompt cache TTL is 5 minutes for the default tier and
# extends with each cache hit. We use 5 min as the baseline and let the
# operator override per session.
DEFAULT_CACHE_TTL_SECONDS = 5 * 60


@dataclass
class CacheUsageSnapshot:
    """One turn's reported cache usage."""

    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    at: float = field(default_factory=time.time)

    @classmethod
    def from_usage_dict(cls, usage: dict | None) -> CacheUsageSnapshot:
        if not usage:
            return cls()
        return cls(
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


@dataclass
class CacheObserver:
    """Accumulates per-session cache usage and computes hit rate.

    Use one observer per session and call `record(usage_dict)` after each
    turn. Inspect `hit_rate()` and `last_hit_at` for telemetry / decisions.
    """

    snapshots: list[CacheUsageSnapshot] = field(default_factory=list)
    total_read: int = 0
    total_create: int = 0
    total_input: int = 0
    total_output: int = 0
    last_hit_at: float | None = None

    def record(self, usage: dict | None) -> CacheUsageSnapshot:
        snap = CacheUsageSnapshot.from_usage_dict(usage)
        self.snapshots.append(snap)
        self.total_read += snap.cache_read_input_tokens
        self.total_create += snap.cache_creation_input_tokens
        self.total_input += snap.input_tokens
        self.total_output += snap.output_tokens
        if snap.cache_read_input_tokens > 0:
            self.last_hit_at = snap.at
        return snap

    def hit_rate(self) -> float:
        denom = self.total_read + self.total_input
        if denom <= 0:
            return 0.0
        return self.total_read / denom

    def cache_alive(
        self, *, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS, now: float | None = None
    ) -> bool:
        """True if the cache is likely still warm (last hit within TTL)."""
        if self.last_hit_at is None:
            return False
        return (now or time.time()) - self.last_hit_at < ttl_seconds

    def summary(self) -> dict[str, float | int]:
        return {
            "turns": len(self.snapshots),
            "cache_read": self.total_read,
            "cache_create": self.total_create,
            "input": self.total_input,
            "output": self.total_output,
            "hit_rate": round(self.hit_rate(), 4),
        }


def should_apply_cache_markers(
    observer: CacheObserver,
    *,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    min_turns_to_evaluate: int = 3,
) -> bool:
    """Decide whether the next turn should still pay for cache markers.

    - For the first few turns: yes (always try to warm the cache).
    - After that: only if the cache is still alive (last hit within TTL)
      and the running hit_rate beat a low floor (5%). Otherwise the
      `cache_creation_input_tokens` cost outweighs the read savings.
    """
    if len(observer.snapshots) < min_turns_to_evaluate:
        return True
    if not observer.cache_alive(ttl_seconds=ttl_seconds):
        return False
    return observer.hit_rate() >= 0.05


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "CacheObserver",
    "CacheUsageSnapshot",
    "should_apply_cache_markers",
]
