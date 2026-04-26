"""NetPolicy — host/port/network-class allow/deny rules.

Mirrors the openclaw `SsrFPolicy` shape with the additions sampyClaw
needs (port allowlist, deny rules). Policies merge with restrictive-AND
semantics for flags and union semantics for lists, so several callers
(channel, skill, tool, global default) can each assert their requirements
and the result is at least as strict as the strictest input.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass, replace


class NetPolicyError(Exception):
    """Raised when a policy is internally inconsistent (e.g. allow == deny)."""


# Standard ports always implicitly allowed (HTTP/HTTPS).
_DEFAULT_PORTS: tuple[int, ...] = (80, 443)


@dataclass(frozen=True)
class NetPolicy:
    """Outbound HTTP/HTTPS policy.

    Hostname matching uses `fnmatch.fnmatchcase` style globs against the
    *lowered* hostname. Patterns are compared case-insensitively. Empty
    `allowed_hostnames` means "all (subject to deny + private-network flag)";
    a non-empty list switches to *strict* mode where unmatched hosts are
    refused.
    """

    allowed_hostnames: tuple[str, ...] = ()
    denied_hostnames: tuple[str, ...] = ()
    allow_private_network: bool = False
    allow_loopback: bool = False
    extra_allowed_ports: tuple[int, ...] = ()
    # Allowed schemes — adding `ws`/`wss` is opt-in.
    allowed_schemes: tuple[str, ...] = ("http", "https")

    def __post_init__(self) -> None:
        # Sanity: a literal that is in BOTH allow and deny is almost
        # certainly a mistake. We refuse rather than silently defaulting.
        for pat in self.allowed_hostnames:
            if pat in self.denied_hostnames:
                raise NetPolicyError(f"hostname pattern {pat!r} appears in both allow and deny")

    # ─── matchers ────────────────────────────────────────────────────

    def is_hostname_allowed(self, hostname: str) -> bool:
        host = hostname.lower()
        if hostname_matches(host, self.denied_hostnames):
            return False
        if not self.allowed_hostnames:
            # No explicit allowlist → permissive (deny still applies).
            return True
        return hostname_matches(host, self.allowed_hostnames)

    def is_port_allowed(self, port: int) -> bool:
        return port in _DEFAULT_PORTS or port in self.extra_allowed_ports

    def is_scheme_allowed(self, scheme: str) -> bool:
        return scheme.lower() in self.allowed_schemes

    # ─── derive ──────────────────────────────────────────────────────

    def with_extra_allow(self, *patterns: str) -> NetPolicy:
        """Return a copy with `patterns` appended to allow list."""
        merged = tuple(dict.fromkeys((*self.allowed_hostnames, *patterns)))
        return replace(self, allowed_hostnames=merged)


def hostname_matches(host: str, patterns: Iterable[str]) -> bool:
    """Glob match `host` against any pattern in `patterns` (case-insensitive)."""
    h = host.lower()
    return any(fnmatch.fnmatchcase(h, pat.lower()) for pat in patterns)


def merge_policies(*policies: NetPolicy | None) -> NetPolicy:
    """Combine multiple policies into one that is at least as strict as
    each.

    - `allowed_hostnames`: **intersection** when both sides are non-empty;
      union of the non-empty ones otherwise. Rationale: "any caller can
      tighten the allowlist; nobody can re-broaden it".
    - `denied_hostnames`: **union**.
    - `allow_private_network` / `allow_loopback`: **AND** (any False wins).
    - `extra_allowed_ports`: **intersection** when both sides non-empty
      (else union with empty preserved as "no extras").
    - `allowed_schemes`: **intersection** of non-empty sets.
    """
    real = [p for p in policies if p is not None]
    if not real:
        return NetPolicy()
    if len(real) == 1:
        return real[0]

    def _merge_allowlist(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
        if not a:
            return b
        if not b:
            return a
        return tuple(p for p in a if p in b)

    def _merge_ports(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
        if not a:
            return b
        if not b:
            return a
        return tuple(p for p in a if p in b)

    def _merge_schemes(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
        if not a:
            return b
        if not b:
            return a
        return tuple(s for s in a if s in b)

    out = real[0]
    for p in real[1:]:
        out = NetPolicy(
            allowed_hostnames=_merge_allowlist(out.allowed_hostnames, p.allowed_hostnames),
            denied_hostnames=tuple(dict.fromkeys((*out.denied_hostnames, *p.denied_hostnames))),
            allow_private_network=out.allow_private_network and p.allow_private_network,
            allow_loopback=out.allow_loopback and p.allow_loopback,
            extra_allowed_ports=_merge_ports(out.extra_allowed_ports, p.extra_allowed_ports),
            allowed_schemes=_merge_schemes(out.allowed_schemes, p.allowed_schemes),
        )
    return out


def _csv(env_value: str | None) -> tuple[str, ...]:
    if not env_value:
        return ()
    return tuple(p.strip() for p in env_value.split(",") if p.strip())


def _ports(env_value: str | None) -> tuple[int, ...]:
    if not env_value:
        return ()
    out: list[int] = []
    for tok in env_value.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return tuple(out)


def policy_from_env(env: dict[str, str] | None = None) -> NetPolicy:
    """Build a baseline policy from `SAMPYCLAW_NET_*` env vars.

    Useful as the *default* policy at process start; per-call policies
    can layer on top via `merge_policies`.
    """
    src = env if env is not None else os.environ
    return NetPolicy(
        allowed_hostnames=_csv(src.get("SAMPYCLAW_NET_ALLOW_HOSTS")),
        denied_hostnames=_csv(src.get("SAMPYCLAW_NET_DENY_HOSTS")),
        allow_private_network=src.get("SAMPYCLAW_NET_ALLOW_PRIVATE", "").lower()
        in ("1", "true", "yes"),
        allow_loopback=src.get("SAMPYCLAW_NET_ALLOW_LOOPBACK", "").lower() in ("1", "true", "yes"),
        extra_allowed_ports=_ports(src.get("SAMPYCLAW_NET_EXTRA_PORTS")),
    )


__all__ = [
    "NetPolicy",
    "NetPolicyError",
    "hostname_matches",
    "merge_policies",
    "policy_from_env",
]
