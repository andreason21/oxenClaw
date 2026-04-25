"""Webhook inbound guards.

Mirrors openclaw `plugin-sdk/webhook-request-guards.ts` +
`webhook-memory-guards.ts`. The three primitives webhook handlers always
need:

- `BodySizeLimiter` — read up to N bytes from a streaming source; raise
  `BodyTooLargeError` if exceeded. Doesn't pre-allocate.
- `FixedWindowRateLimiter` — request count per key per window; pure
  in-memory (no external store). Sliding-window variant available.
- `verify_hmac_signature` — HMAC-SHA256 constant-time compare.

Plus a `WebhookProfile` enum that bundles tightened limits for the
*pre-auth* path (where any anonymous caller can hit the endpoint) vs the
*post-auth* path (where the caller has already proved identity).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BodyTooLargeError(RuntimeError):
    """Raised by `BodySizeLimiter.read_with_limit` when the body exceeds
    its budget."""


class RateLimited(RuntimeError):
    """Raised when a key has been blocked by a rate limiter."""

    def __init__(self, key: str, retry_after: float) -> None:
        super().__init__(f"rate limited: {key!r} (retry in {retry_after:.1f}s)")
        self.key = key
        self.retry_after = retry_after


# ─── Body size limiter ──────────────────────────────────────────────


class BodySizeLimiter:
    """Cap incoming request body to `max_bytes`."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self._max = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max

    async def read_streaming(
        self, stream: AsyncIterator[bytes]
    ) -> bytes:
        """Aggregate `stream` into one bytes blob, raising if cap exceeded."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            total += len(chunk)
            if total > self._max:
                raise BodyTooLargeError(
                    f"body exceeded {self._max} bytes (got at least {total})"
                )
            chunks.append(chunk)
        return b"".join(chunks)


# ─── Rate limiter ───────────────────────────────────────────────────


@dataclass
class _Bucket:
    count: int = 0
    window_start: float = 0.0


class FixedWindowRateLimiter:
    """Per-key fixed-window counter.

    Cheap, easy to reason about, slight burstiness at window edges (the
    standard caveat). When that's a problem, use `SlidingWindowRateLimiter`
    below. Keys older than `eviction_after_seconds` are removed lazily on
    `check()` to bound memory growth (mirrors openclaw's
    `pruneMapToMaxSize`).
    """

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: float,
        max_keys: int = 10_000,
        clock: Any = time.monotonic,
    ) -> None:
        if max_requests <= 0 or window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be > 0")
        self._max_requests = max_requests
        self._window = window_seconds
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._clock = clock

    def check(self, key: str) -> bool:
        """Return True if the request is allowed; False if the key is over
        its budget for the current window."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._maybe_prune()
            bucket = _Bucket(count=0, window_start=now)
            self._buckets[key] = bucket
        elif now - bucket.window_start >= self._window:
            bucket.window_start = now
            bucket.count = 0
        if bucket.count >= self._max_requests:
            return False
        bucket.count += 1
        return True

    def assert_allowed(self, key: str) -> None:
        if not self.check(key):
            bucket = self._buckets.get(key)
            window_left = (
                bucket.window_start + self._window - self._clock()
                if bucket
                else self._window
            )
            raise RateLimited(key, max(0.0, window_left))

    def _maybe_prune(self) -> None:
        """LRU-style trim when bucket count crosses `max_keys`."""
        if len(self._buckets) < self._max_keys:
            return
        # Sort by window_start ascending; drop oldest 25%.
        items = sorted(self._buckets.items(), key=lambda kv: kv[1].window_start)
        drop = max(1, len(items) // 4)
        for key, _ in items[:drop]:
            self._buckets.pop(key, None)

    def __len__(self) -> int:
        return len(self._buckets)


# ─── HMAC signature verification ────────────────────────────────────


def verify_hmac_signature(
    secret: str | bytes,
    body: bytes,
    provided_signature: str,
    *,
    digest: str = "sha256",
    prefix: str = "",
) -> bool:
    """Constant-time HMAC compare.

    `provided_signature` may include a provider prefix like
    `sha256=<hex>` (GitHub) — pass `prefix="sha256="` to strip it before
    compare.
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    expected = hmac.new(secret, body, getattr(hashlib, digest)).hexdigest()
    if prefix and provided_signature.startswith(prefix):
        provided_signature = provided_signature[len(prefix) :]
    return hmac.compare_digest(expected, provided_signature)


# ─── Profile bundle ─────────────────────────────────────────────────


class WebhookProfile(str, Enum):
    PRE_AUTH = "pre_auth"
    POST_AUTH = "post_auth"


@dataclass(frozen=True)
class WebhookGuards:
    """Combined limits for one endpoint."""

    body_limiter: BodySizeLimiter
    rate_limiter: FixedWindowRateLimiter
    hmac_secret: str | None = None
    hmac_header: str = "X-Signature"
    hmac_prefix: str = "sha256="

    def verify_signature(self, body: bytes, headers: dict[str, str]) -> bool:
        if not self.hmac_secret:
            # No secret configured → caller should explicitly use a no-auth
            # flow; we don't silently pass.
            return False
        sig = headers.get(self.hmac_header) or headers.get(self.hmac_header.lower())
        if not sig:
            return False
        return verify_hmac_signature(
            self.hmac_secret, body, sig, prefix=self.hmac_prefix
        )


def default_guards(profile: WebhookProfile) -> WebhookGuards:
    """Reasonable defaults that match openclaw's pre/post-auth split."""
    if profile is WebhookProfile.PRE_AUTH:
        return WebhookGuards(
            body_limiter=BodySizeLimiter(max_bytes=64 * 1024),  # 64 KiB
            rate_limiter=FixedWindowRateLimiter(
                max_requests=30, window_seconds=60
            ),
        )
    return WebhookGuards(
        body_limiter=BodySizeLimiter(max_bytes=4 * 1024 * 1024),  # 4 MiB
        rate_limiter=FixedWindowRateLimiter(
            max_requests=600, window_seconds=60
        ),
    )


__all__ = [
    "BodySizeLimiter",
    "BodyTooLargeError",
    "FixedWindowRateLimiter",
    "RateLimited",
    "WebhookGuards",
    "WebhookProfile",
    "default_guards",
    "verify_hmac_signature",
]
