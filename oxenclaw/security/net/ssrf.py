"""Hardened SSRF host classification.

Mirrors openclaw `infra/net/ssrf.ts` IP classifiers — covers:
- IPv4 special-use blocks (0.0.0.0/8, 127/8, 10/8, 172.16/12, 192.168/16,
  169.254/16, 100.64/10 CGNAT, 198.18/15 RFC2544 benchmark, 224/4 multicast,
  240/4 reserved, 255.255.255.255 broadcast).
- IPv6 special-use blocks (::1, fc00::/7 ULA, fe80::/10 link-local, multicast,
  IPv4-mapped extraction).
- Loose-IPv4 literals (0x7f.0.0.1, 0177.0.0.1, 2130706433 — refuse them
  outright since they're commonly used in SSRF bypass attacks).

Combines with `NetPolicy` so callers can opt into private-network access
explicitly when they have a legitimate reason (operator dashboard, etc).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from oxenclaw.security.net.policy import NetPolicy


class SsrFBlockedError(RuntimeError):
    """Raised when a URL/host fails policy."""


# Commonly-used SSRF bypass tricks: hex/octal IPv4, decimal IPv4 (single
# integer), trailing dot. We reject all "loose" forms; only canonical
# dotted-decimal is acceptable.
_CANONICAL_IPV4_RE = re.compile(
    r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$"
)


def is_canonical_ipv4(host: str) -> bool:
    return _CANONICAL_IPV4_RE.match(host) is not None


def is_loose_ipv4_literal(host: str) -> bool:
    """Detect SSRF-bypass-style IPv4 literals.

    True only when `host` parses as IPv4 under the libc-permissive rules
    (`socket.inet_aton` accepts hex/octal/short/trailing-dot forms) but is
    NOT in canonical dotted-decimal form. Real hostnames like
    `query1.finance.yahoo.com` never reach `inet_aton` success and so are
    correctly classified as not-an-IP-literal.

    Examples we want to refuse (return True):
    - `0x7f.0.0.1`     (hex octet)
    - `0177.0.0.1`     (octal octet)
    - `2130706433`     (single decimal)
    - `127.1`          (short form)
    - `127.0.0.1.`     (trailing dot)
    """
    # Strip a single trailing dot — `127.0.0.1.` is a loose form even
    # though `inet_aton` on glibc rejects it.
    h = host.rstrip(".")
    if h != host and (is_canonical_ipv4(h) or _inet_aton_accepts(h)):
        return True
    if is_canonical_ipv4(host):
        return False
    return _inet_aton_accepts(host)


def _inet_aton_accepts(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


def is_ipv4_in_ipv6(addr: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract embedded IPv4 from `::ffff:1.2.3.4` and similar."""
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr.teredo is not None:
        # Teredo client IP is the second half.
        return addr.teredo[1]
    return None


def classify_blocked(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool,
    allow_loopback: bool,
) -> str | None:
    """Return a reason string if `ip` should be blocked, else None."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = is_ipv4_in_ipv6(ip)
        if embedded is not None:
            inner = classify_blocked(
                embedded, allow_private=allow_private, allow_loopback=allow_loopback
            )
            if inner:
                return f"IPv6-embedded IPv4 {embedded}: {inner}"
        if ip.is_loopback and not allow_loopback:
            return "IPv6 loopback (::1)"
        if ip.is_link_local:
            return "IPv6 link-local (fe80::/10)"
        if ip.is_multicast:
            return "IPv6 multicast"
        if ip.is_private and not allow_private:
            # Includes ULA fc00::/7.
            return "IPv6 private/ULA"
        if ip.is_reserved:
            return "IPv6 reserved"
        return None

    # IPv4
    if ip.is_loopback and not allow_loopback:
        return "IPv4 loopback (127/8)"
    if ip.is_link_local:
        return "IPv4 link-local (169.254/16)"
    if ip.is_multicast:
        return "IPv4 multicast"
    if ip.is_reserved:
        return "IPv4 reserved (240/4)"
    if str(ip) == "255.255.255.255":
        return "IPv4 broadcast"
    if ip.packed[0] == 0:
        return "IPv4 0.0.0.0/8"
    # CGNAT 100.64.0.0/10
    if ip.packed[0] == 100 and 64 <= ip.packed[1] < 128:
        return "IPv4 CGNAT (100.64/10)"
    # RFC2544 benchmark 198.18.0.0/15
    if ip.packed[0] == 198 and ip.packed[1] in (18, 19):
        return "IPv4 RFC2544 benchmark (198.18/15)"
    if ip.is_private and not allow_private:
        return "IPv4 private (RFC1918)"
    return None


def assert_url_allowed(url: str, policy: NetPolicy) -> str:
    """Validate URL against `policy`. Returns the parsed hostname on success;
    raises `SsrFBlockedError` otherwise."""
    parsed = urlparse(url)
    if not policy.is_scheme_allowed(parsed.scheme or ""):
        raise SsrFBlockedError(f"scheme {parsed.scheme!r} not in {policy.allowed_schemes}")
    host = parsed.hostname
    if not host:
        raise SsrFBlockedError("URL has no host")
    if parsed.port is not None and not policy.is_port_allowed(parsed.port):
        raise SsrFBlockedError(f"port {parsed.port} not allowed by policy")

    # Refuse loose IPv4 literals before doing anything else. The classifier
    # itself only returns True for hosts that actually parse as IPv4 under
    # libc-permissive rules, so passing real hostnames through is safe.
    if is_loose_ipv4_literal(host):
        raise SsrFBlockedError(f"loose IPv4 literal refused: {host!r}")

    # If the host is an IP literal (canonical IPv4 or any IPv6), classify
    # immediately so URLs like `http://10.0.0.1/` are caught at pre-flight
    # rather than deferred to the resolver.
    try:
        ip_literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip_literal = None
    if ip_literal is not None:
        reason = classify_blocked(
            ip_literal,
            allow_private=policy.allow_private_network,
            allow_loopback=policy.allow_loopback,
        )
        if reason is not None:
            raise SsrFBlockedError(f"URL host {host!r} blocked: {reason}")

    if not policy.is_hostname_allowed(host):
        raise SsrFBlockedError(f"hostname {host!r} not allowed by policy")
    return host


def assert_ip_allowed(
    ip_str: str, *, policy: NetPolicy, hostname: str
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Validate a *resolved* IP against the policy. Returns the parsed IP."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as exc:
        raise SsrFBlockedError(f"unparseable IP {ip_str!r} for {hostname!r}: {exc}") from exc
    reason = classify_blocked(
        ip,
        allow_private=policy.allow_private_network,
        allow_loopback=policy.allow_loopback,
    )
    if reason is not None:
        raise SsrFBlockedError(f"{hostname!r} resolves to {ip} which is blocked: {reason}")
    return ip


__all__ = [
    "SsrFBlockedError",
    "assert_ip_allowed",
    "assert_url_allowed",
    "classify_blocked",
    "is_canonical_ipv4",
    "is_ipv4_in_ipv6",
    "is_loose_ipv4_literal",
]
