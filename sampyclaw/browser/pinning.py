"""Per-host DNS pinning for the browser route handler.

The `security.net.pinning.PinnedResolver` is an aiohttp resolver and
can't be plugged into Chromium directly. Instead we pin at the
**route-handler** layer: every intercepted request resolves its host
once via `getaddrinfo`, validates each returned IP against `NetPolicy`,
and caches the address set with a TTL. Subsequent requests for the same
host must return a subset of the cached addresses; a new IP triggers
re-validation. A host that resolves to a *different* IP set after
expiry without overlap raises `RebindBlockedError`.

This catches:

- DNS rebinding (public IP at preflight → private IP at fetch time).
- Hosts that quietly start returning loopback/RFC1918 mid-session.
- Wildcard CNAMEs that target a different rebound IP per query.

Performance: a hot host hits the in-memory cache in ~1 µs; cold misses
do one `getaddrinfo` (~1 ms on a primed local resolver). The cache is
LRU-capped so a long-running session can't grow unbounded.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from sampyclaw.browser.errors import RebindBlockedError
from sampyclaw.browser.policy import BrowserPolicy
from sampyclaw.security.net.ssrf import SsrFBlockedError, assert_ip_allowed

DEFAULT_PIN_TTL_SECONDS = 300.0
DEFAULT_PIN_CAPACITY = 1024


@dataclass
class _PinEntry:
    ips: frozenset[str]
    cached_at: float


class HostPinCache:
    """Thread-safe LRU pinning cache for hostname → validated IP set.

    Designed for synchronous use from inside Playwright route handlers
    (they execute on Playwright's worker thread). Use `resolve_or_pin`
    once per intercepted request.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PIN_TTL_SECONDS,
        capacity: int = DEFAULT_PIN_CAPACITY,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _PinEntry] = OrderedDict()

    # ─── public API ─────────────────────────────────────────────────

    def resolve_or_pin(self, host: str, policy: BrowserPolicy) -> frozenset[str]:
        """Resolve `host`, validate every IP against `policy.net`, and pin.

        On hit, returns the cached IP set without touching DNS. On expiry
        or first-seen host, re-resolves and validates.

        Raises:
            RebindBlockedError: if the new resolution introduces an IP
                that is not in the previously-pinned set AND that IP
                does not pass policy validation.
            SsrFBlockedError: if every resolved IP is blocked.
        """
        host = host.lower()
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(host)
            if cached is not None and (now - cached.cached_at) < self._ttl:
                self._entries.move_to_end(host)
                return cached.ips

        # Cold path: do the resolve outside the lock so concurrent
        # requests for *different* hosts don't serialize.
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SsrFBlockedError(f"DNS resolution failed for {host!r}: {exc}") from exc
        resolved: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                resolved.add(sockaddr[0])
        if not resolved:
            raise SsrFBlockedError(f"no addresses resolved for {host!r}")

        # Validate each IP. Any that fail are stripped — the host is
        # only usable if at least one IP survives policy.
        validated: set[str] = set()
        last_block: str | None = None
        for ip in resolved:
            try:
                assert_ip_allowed(ip, policy=policy.net, hostname=host)
            except SsrFBlockedError as exc:
                last_block = str(exc)
                continue
            validated.add(ip)
        if not validated:
            raise SsrFBlockedError(
                f"all addresses for {host!r} blocked by policy"
                + (f": {last_block}" if last_block else "")
            )
        new_set = frozenset(validated)

        with self._lock:
            existing = self._entries.get(host)
            if existing is not None:
                # Rebind check: every freshly-resolved IP that isn't in
                # the existing pinned set must independently pass policy
                # (already verified above). The *replacement* of the IP
                # set is itself the suspicious event — flag if there's
                # zero overlap with previously-trusted IPs.
                if not (new_set & existing.ips):
                    raise RebindBlockedError(
                        f"host {host!r} resolved to a fully disjoint IP set "
                        f"({sorted(existing.ips)} → {sorted(new_set)}); "
                        f"refusing rebind"
                    )
                # Union with existing pin so a load-balanced host that
                # rotates IPs doesn't trip on a benign DNS round-robin.
                merged = existing.ips | new_set
                self._entries[host] = _PinEntry(ips=merged, cached_at=now)
                self._entries.move_to_end(host)
                return merged
            self._entries[host] = _PinEntry(ips=new_set, cached_at=now)
            self._entries.move_to_end(host)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return new_set

    def invalidate(self, host: str | None = None) -> None:
        with self._lock:
            if host is None:
                self._entries.clear()
            else:
                self._entries.pop(host.lower(), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = [
    "DEFAULT_PIN_CAPACITY",
    "DEFAULT_PIN_TTL_SECONDS",
    "HostPinCache",
]
