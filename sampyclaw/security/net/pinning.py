"""DNS-pinning resolver — defends against DNS rebinding.

Mirrors openclaw `createPinnedDispatcher`. Standard aiohttp resolves the
hostname for every request; an attacker controlling DNS could return a
public IP for the policy-check resolution and a private IP for the
actual connect (DNS rebinding).

`PinnedResolver`:
1. Resolves the hostname **once**.
2. Validates every returned IP against `NetPolicy` + `assert_ip_allowed`.
3. Caches the resolved IP set with TTL.
4. On subsequent calls, returns the cached IP set — connect is forced
   onto a previously-validated address, regardless of what DNS now says.
5. On TTL expiry, re-resolves; if the IP set changes in a way that
   includes a previously-unseen address, the *new* address must also
   pass policy validation.

The resolver also rejects Happy-Eyeballs surprises: if the first resolve
returned IPv4 only, a later IPv6-only response is treated as a fresh
validation event, not silent acceptance.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import aiohttp

from sampyclaw.security.net.policy import NetPolicy
from sampyclaw.security.net.ssrf import SsrFBlockedError, assert_ip_allowed

DEFAULT_TTL_SECONDS = 60.0


@dataclass
class _PinnedEntry:
    addresses: tuple[str, ...]
    family: int
    cached_at: float


class PinnedResolver(aiohttp.abc.AbstractResolver):
    """aiohttp resolver that caches + validates IPs against `NetPolicy`."""

    def __init__(
        self,
        policy: NetPolicy,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._policy = policy
        self._ttl = ttl_seconds
        self._loop = loop
        self._cache: dict[tuple[str, int], _PinnedEntry] = {}
        # Per-host validated IP set, accumulated across re-resolves so we
        # can detect "new IP appeared" without throwing away history.
        self._validated_ips: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> NetPolicy:
        return self._policy

    def replace_policy(self, policy: NetPolicy) -> None:
        """Hot-swap the policy. Cached entries remain but are re-validated
        on next access. Useful for tests + dynamic operator updates."""
        self._policy = policy
        self._cache.clear()
        self._validated_ips.clear()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        # aiohttp's expected return shape: [{"hostname": str, "host": str
        # (= ip), "port": int, "family": int, "proto": int, "flags": int}, ...]
        async with self._lock:
            return await self._resolve_locked(host, port, family)

    async def _resolve_locked(self, host: str, port: int, family: int) -> list[dict[str, Any]]:
        cache_key = (host, family)
        cached = self._cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached.cached_at < self._ttl:
            return self._format(host, cached.addresses, port, family)

        loop = self._loop or asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(
                host, port or None, type=socket.SOCK_STREAM, family=family
            )
        except socket.gaierror as exc:
            raise SsrFBlockedError(f"DNS resolution failed for {host!r}: {exc}") from exc

        addresses: list[str] = []
        for fam, _socktype, _proto, _canon, sockaddr in infos:
            if fam not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip_str = sockaddr[0]
            # Validate against policy now. If we've validated this IP for
            # this host before, fast-path skip — same IP, same verdict.
            seen = self._validated_ips.setdefault(host, set())
            if ip_str not in seen:
                # Raises SsrFBlockedError on rejection — caller sees the
                # specific IP + reason.
                assert_ip_allowed(ip_str, policy=self._policy, hostname=host)
                seen.add(ip_str)
            addresses.append(ip_str)

        if not addresses:
            raise SsrFBlockedError(f"no usable addresses returned for {host!r}")
        unique_addrs = tuple(dict.fromkeys(addresses))
        self._cache[cache_key] = _PinnedEntry(addresses=unique_addrs, family=family, cached_at=now)
        return self._format(host, unique_addrs, port, family)

    def _format(
        self, host: str, addrs: Iterable[str], port: int, family: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "hostname": host,
                "host": addr,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
            for addr in addrs
        ]

    async def close(self) -> None:
        self._cache.clear()
        self._validated_ips.clear()


def make_guarded_connector(
    policy: NetPolicy,
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    limit: int = 100,
) -> aiohttp.TCPConnector:
    """Build a `TCPConnector` that uses `PinnedResolver(policy)`.

    Use this anywhere a session needs to honour the global net policy:

        connector = make_guarded_connector(policy)
        async with aiohttp.ClientSession(connector=connector) as session:
            ...
    """
    resolver = PinnedResolver(policy, ttl_seconds=ttl_seconds)
    return aiohttp.TCPConnector(resolver=resolver, limit=limit, ttl_dns_cache=int(ttl_seconds))


__all__ = ["DEFAULT_TTL_SECONDS", "PinnedResolver", "make_guarded_connector"]
