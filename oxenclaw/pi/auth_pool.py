"""Auth-key pools — multi-key rotation + automatic failover.

Wraps an underlying single-key `AuthStorage` so callers can:

  - Register N keys for the same provider (`pool.add(provider, key)`).
  - Pull a key by configurable strategy via `await pool.get(...)`:
    `round_robin` (default), `fill_first` (best for prompt-cache-
    sensitive providers like Anthropic — keep hitting the same key
    so cache prefix stays warm), `least_used`, or `random`.
  - On a 401 / 429 / 5xx, mark the current key bad with
    `await pool.report_failure(...)` and re-fetch — the pool advances
    past the dead key automatically.
  - Pin a specific key for a session via `pool.lock(provider, key_id)`
    so cache-prefix-sensitive providers (Anthropic) keep hitting the
    same key for cache reuse.
  - Honour a provider-supplied `Retry-After` (or `x-ratelimit-reset-*`)
    via `report_failure(retry_after_seconds=...)` instead of the
    default cooldown. Prevents retry storms during a long quota
    window — observed when a 429 says "retry in 4h" but the default
    60s cooldown would otherwise re-issue the same request 240×.

Mirrors the substance of openclaw `auth-profiles/` (multi-profile
auth store + lock + rotation) without porting the on-disk schema —
oxenclaw's `EnvAuthStorage` and the Tauri keychain backend stay the
single-key path; AuthKeyPool sits between them and `resolve_api`.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.auth_pool")


PoolStrategy = Literal["round_robin", "fill_first", "least_used", "random"]


@dataclass
class _PoolEntry:
    """One key in a per-provider rotation."""

    key_id: str  # short label (e.g. "primary", "backup-1")
    api_key: str
    failure_count: int = 0
    last_failure_ts: float | None = None
    cooldown_until: float = 0.0  # epoch seconds; 0 = active
    use_count: int = 0  # for least_used strategy

    @property
    def is_alive(self) -> bool:
        return time.time() >= self.cooldown_until


@dataclass
class AuthKeyPool:
    """Multi-strategy pool with cooldown-on-failure.

    `cooldown_seconds` defaults to 60s — the time we'll skip a key
    after a 401/429/5xx before retrying it. Three consecutive
    failures bumps the cooldown to `cooldown_seconds * 5`. A caller
    that has parsed `Retry-After` (or `x-ratelimit-reset-*`) can
    override per-call via `report_failure(retry_after_seconds=...)`.

    `strategy` controls which alive key `get()` returns:
    - `round_robin`: legacy default, cycles through keys.
    - `fill_first`: always return the first alive key — Anthropic-
      friendly because the prompt cache stays warm on one key.
    - `least_used`: pick the alive key with the lowest `use_count`.
    - `random`: pick a random alive key.
    """

    cooldown_seconds: float = 60.0
    strategy: PoolStrategy = "round_robin"
    by_provider: dict[str, list[_PoolEntry]] = field(default_factory=dict)
    cursor: dict[str, int] = field(default_factory=dict)
    locked_key_id: dict[str, str] = field(default_factory=dict)

    def add(self, provider: str, key_id: str, api_key: str) -> None:
        self.by_provider.setdefault(provider, []).append(_PoolEntry(key_id=key_id, api_key=api_key))

    def lock(self, provider: str, key_id: str) -> None:
        """Pin the active key — useful for cache-prefix-sensitive runs."""
        self.locked_key_id[provider] = key_id

    def unlock(self, provider: str) -> None:
        self.locked_key_id.pop(provider, None)

    async def get(self, provider: str) -> tuple[str, str] | None:
        """Return `(key_id, api_key)` per `self.strategy`, or None.

        Skips any entry whose cooldown hasn't expired. Returns None when
        every key is in cooldown — caller should fall back to env-var
        auth or report failure to the user."""
        entries = self.by_provider.get(provider, [])
        if not entries:
            return None
        # Locked path: always return the pinned key (even if cooling).
        locked_id = self.locked_key_id.get(provider)
        if locked_id is not None:
            for e in entries:
                if e.key_id == locked_id:
                    e.use_count += 1
                    return (e.key_id, e.api_key)
        alive = [(idx, e) for idx, e in enumerate(entries) if e.is_alive]
        if not alive:
            return None
        if self.strategy == "fill_first":
            # Anthropic-friendly: keep hitting the same warm key so the
            # provider's prompt cache prefix stays valid. Only advance
            # when the head goes into cooldown.
            _idx, entry = alive[0]
        elif self.strategy == "least_used":
            _idx, entry = min(alive, key=lambda x: x[1].use_count)
        elif self.strategy == "random":
            _idx, entry = random.choice(alive)
        else:  # round_robin
            n = len(entries)
            start = self.cursor.get(provider, 0) % n
            picked: tuple[int, _PoolEntry] | None = None
            for offset in range(n):
                cand_idx = (start + offset) % n
                cand = entries[cand_idx]
                if cand.is_alive:
                    picked = (cand_idx, cand)
                    self.cursor[provider] = (cand_idx + 1) % n
                    break
            if picked is None:
                return None
            _idx, entry = picked
        entry.use_count += 1
        return (entry.key_id, entry.api_key)

    async def report_failure(
        self,
        provider: str,
        key_id: str,
        *,
        status: int | None = None,
        message: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Mark a key as failing and apply cooldown.

        When `retry_after_seconds` is provided (parsed from a provider's
        `Retry-After` header, or computed from `x-ratelimit-reset-*`),
        we use that value verbatim instead of the default 60s/300s
        ladder — capped to one hour to bound damage from broken
        provider responses. This prevents retry storms during long
        quota windows where the default cooldown would otherwise
        re-issue 240× requests/hour against a wall.

        Idempotent — calling repeatedly with the same key_id within a
        short window doesn't multiply the cooldown. Logs at WARNING
        so operators see flapping keys.
        """
        entries = self.by_provider.get(provider, [])
        for e in entries:
            if e.key_id != key_id:
                continue
            now = time.time()
            # Coalesce burst failures (within 5s) so retry storms
            # don't push the cooldown into the next century.
            if e.last_failure_ts is not None and now - e.last_failure_ts < 5.0:
                logger.info(
                    "auth_pool: ignoring duplicate failure for %s/%s",
                    provider,
                    key_id,
                )
                return
            e.failure_count += 1
            e.last_failure_ts = now
            if retry_after_seconds is not None and retry_after_seconds > 0:
                cd = min(float(retry_after_seconds), 3600.0)
            else:
                cd = self.cooldown_seconds * (5 if e.failure_count >= 3 else 1)
            e.cooldown_until = now + cd
            logger.warning(
                "auth_pool: %s/%s failed (status=%s, msg=%s) — cooldown=%.0fs streak=%d retry_after=%s",
                provider,
                key_id,
                status,
                message,
                cd,
                e.failure_count,
                retry_after_seconds,
            )
            return

    async def report_success(self, provider: str, key_id: str) -> None:
        """Reset failure streak on a successful call."""
        for e in self.by_provider.get(provider, []):
            if e.key_id == key_id and e.failure_count:
                e.failure_count = 0
                e.cooldown_until = 0.0

    def keys_for(self, provider: str) -> list[dict[str, Any]]:
        """Inspector view for `agents.auth_status` RPC."""
        out = []
        for e in self.by_provider.get(provider, []):
            out.append(
                {
                    "key_id": e.key_id,
                    "alive": e.is_alive,
                    "failure_count": e.failure_count,
                    "cooldown_until": e.cooldown_until,
                    "last_failure_ts": e.last_failure_ts,
                    "use_count": e.use_count,
                }
            )
        return out


__all__ = ["AuthKeyPool", "PoolStrategy"]
