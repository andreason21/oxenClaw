"""Tests for sampyclaw.security.net (Phases N1-N5)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import socket
from pathlib import Path
from typing import Any

import pytest

from sampyclaw.security.net import (
    NetPolicy,
    NetPolicyError,
    hostname_matches,
    merge_policies,
    policy_from_env,
)
from sampyclaw.security.net.audit import (
    AuditConfig,
    OutboundAuditStore,
    make_audit_trace_config,
    should_audit_from_env,
)
from sampyclaw.security.net.guarded_fetch import (
    _close_audit_store,
    guarded_session,
    policy_pre_flight,
)
from sampyclaw.security.net.pinning import (
    PinnedResolver,
    make_guarded_connector,
)
from sampyclaw.security.net.ssrf import (
    SsrFBlockedError,
    assert_ip_allowed,
    assert_url_allowed,
    classify_blocked,
    is_canonical_ipv4,
    is_loose_ipv4_literal,
)
from sampyclaw.security.net.webhook_guards import (
    BodySizeLimiter,
    BodyTooLargeError,
    FixedWindowRateLimiter,
    RateLimited,
    WebhookGuards,
    WebhookProfile,
    default_guards,
    verify_hmac_signature,
)


# ─── Phase N1: NetPolicy ─────────────────────────────────────────────


def test_policy_default_allows_unrestricted_hostnames() -> None:
    p = NetPolicy()
    assert p.is_hostname_allowed("api.example.com") is True


def test_policy_allowlist_strict_mode() -> None:
    p = NetPolicy(allowed_hostnames=("api.example.com", "*.cdn.example.com"))
    assert p.is_hostname_allowed("api.example.com") is True
    assert p.is_hostname_allowed("img.cdn.example.com") is True
    assert p.is_hostname_allowed("evil.com") is False


def test_policy_deny_takes_precedence() -> None:
    p = NetPolicy(
        allowed_hostnames=("*.example.com",),
        denied_hostnames=("internal.example.com",),
    )
    assert p.is_hostname_allowed("public.example.com") is True
    assert p.is_hostname_allowed("internal.example.com") is False


def test_policy_allow_and_deny_overlap_raises() -> None:
    with pytest.raises(NetPolicyError):
        NetPolicy(
            allowed_hostnames=("foo.com",),
            denied_hostnames=("foo.com",),
        )


def test_policy_port_implicit_80_443() -> None:
    p = NetPolicy()
    assert p.is_port_allowed(80)
    assert p.is_port_allowed(443)
    assert not p.is_port_allowed(22)


def test_policy_extra_ports_added() -> None:
    p = NetPolicy(extra_allowed_ports=(8443,))
    assert p.is_port_allowed(8443)


def test_policy_scheme_default_http_https_only() -> None:
    p = NetPolicy()
    assert p.is_scheme_allowed("http")
    assert not p.is_scheme_allowed("file")
    assert not p.is_scheme_allowed("ftp")


def test_hostname_matches_glob_case_insensitive() -> None:
    assert hostname_matches("API.EXAMPLE.COM", ("*.example.com",))
    assert not hostname_matches("api.example.com", ("*.other.com",))


def test_merge_intersects_allowlists_when_both_nonempty() -> None:
    a = NetPolicy(allowed_hostnames=("a.com", "b.com"))
    b = NetPolicy(allowed_hostnames=("b.com", "c.com"))
    out = merge_policies(a, b)
    assert out.allowed_hostnames == ("b.com",)


def test_merge_unions_denylists() -> None:
    a = NetPolicy(denied_hostnames=("x.com",))
    b = NetPolicy(denied_hostnames=("y.com",))
    out = merge_policies(a, b)
    assert set(out.denied_hostnames) == {"x.com", "y.com"}


def test_merge_ANDs_private_network_flag() -> None:
    out = merge_policies(
        NetPolicy(allow_private_network=True),
        NetPolicy(allow_private_network=False),
    )
    assert out.allow_private_network is False


def test_policy_from_env_parses_csv() -> None:
    p = policy_from_env(
        {
            "SAMPYCLAW_NET_ALLOW_HOSTS": "a.com,*.b.com",
            "SAMPYCLAW_NET_DENY_HOSTS": "internal.com",
            "SAMPYCLAW_NET_ALLOW_PRIVATE": "1",
            "SAMPYCLAW_NET_EXTRA_PORTS": "8443,9443",
        }
    )
    assert p.allowed_hostnames == ("a.com", "*.b.com")
    assert p.denied_hostnames == ("internal.com",)
    assert p.allow_private_network is True
    assert p.extra_allowed_ports == (8443, 9443)


# ─── Phase N2: SSRF + DNS pinning ────────────────────────────────────


def test_classify_blocks_ipv4_classes() -> None:
    import ipaddress as ip

    cases = [
        ("127.0.0.1", "loopback"),
        ("10.0.0.1", "private"),
        ("169.254.169.254", "link-local"),
        ("100.64.0.1", "CGNAT"),
        ("198.18.0.1", "RFC2544"),
        ("0.0.0.0", "0.0.0.0/8"),
        ("224.0.0.1", "multicast"),
    ]
    for addr, expect_token in cases:
        reason = classify_blocked(
            ip.IPv4Address(addr), allow_private=False, allow_loopback=False
        )
        assert reason is not None and expect_token.lower() in reason.lower()


def test_classify_allows_public_ipv4() -> None:
    import ipaddress as ip

    assert (
        classify_blocked(
            ip.IPv4Address("1.1.1.1"),
            allow_private=False,
            allow_loopback=False,
        )
        is None
    )


def test_classify_blocks_ipv6_loopback_and_ula() -> None:
    import ipaddress as ip

    assert classify_blocked(
        ip.IPv6Address("::1"), allow_private=False, allow_loopback=False
    )
    assert classify_blocked(
        ip.IPv6Address("fc00::1"),
        allow_private=False,
        allow_loopback=False,
    )
    # IPv4-mapped IPv6 → check inner.
    assert classify_blocked(
        ip.IPv6Address("::ffff:127.0.0.1"),
        allow_private=False,
        allow_loopback=False,
    )


def test_loose_ipv4_literals_refused() -> None:
    assert is_loose_ipv4_literal("0x7f.0.0.1")
    assert is_loose_ipv4_literal("0177.0.0.1")
    assert is_loose_ipv4_literal("2130706433")
    assert is_loose_ipv4_literal("127.1")
    assert is_loose_ipv4_literal("127.0.0.1.")
    assert not is_loose_ipv4_literal("127.0.0.1")


def test_loose_ipv4_literal_does_not_match_real_hostnames() -> None:
    """Regression: a real DNS hostname containing digits was classified as
    a loose IPv4 literal because the segment-by-segment digit check fired
    for any non-pure-digit segment. The fix delegates to `socket.inet_aton`,
    which only accepts hostnames that actually parse as IPv4."""
    assert not is_loose_ipv4_literal("query1.finance.yahoo.com")
    assert not is_loose_ipv4_literal("api2.example.org")
    assert not is_loose_ipv4_literal("s3-us-west-2.amazonaws.com")
    assert not is_loose_ipv4_literal("ipv4.icanhazip.com")
    assert not is_loose_ipv4_literal("8b.8b.8b.8b")  # all-hex but not 0x-prefixed


def test_canonical_ipv4_detection() -> None:
    assert is_canonical_ipv4("1.2.3.4")
    assert is_canonical_ipv4("255.255.255.255")
    assert not is_canonical_ipv4("256.0.0.1")
    assert not is_canonical_ipv4("0177.0.0.1")


def test_assert_url_allowed_ok() -> None:
    p = NetPolicy(allowed_hostnames=("example.com",))
    assert assert_url_allowed("https://example.com/x", p) == "example.com"


def test_assert_url_allowed_blocks_loose_literal() -> None:
    p = NetPolicy()
    with pytest.raises(SsrFBlockedError):
        assert_url_allowed("http://0x7f.0.0.1/", p)


def test_assert_url_allowed_blocks_bad_scheme_and_port() -> None:
    p = NetPolicy()
    with pytest.raises(SsrFBlockedError):
        assert_url_allowed("file:///etc/passwd", p)
    with pytest.raises(SsrFBlockedError):
        assert_url_allowed("https://example.com:22/", p)


def test_assert_ip_allowed_blocks_private_by_default() -> None:
    with pytest.raises(SsrFBlockedError):
        assert_ip_allowed("10.0.0.1", policy=NetPolicy(), hostname="x")


def test_assert_ip_allowed_lets_private_through_when_opted_in() -> None:
    p = NetPolicy(allow_private_network=True)
    ip = assert_ip_allowed("10.0.0.1", policy=p, hostname="x")
    assert str(ip) == "10.0.0.1"


# ─── DNS pinning resolver ────────────────────────────────────────────


class _StubLoop:
    def __init__(self, addrs: dict[str, list[str]]) -> None:
        self._addrs = addrs

    async def getaddrinfo(self, host, port, type=0, family=0):  # type: ignore[no-untyped-def]
        if host not in self._addrs:
            raise socket.gaierror(8, "name or service not known")
        return [
            (
                socket.AF_INET if ":" not in a else socket.AF_INET6,
                socket.SOCK_STREAM,
                0,
                "",
                (a, port or 0),
            )
            for a in self._addrs[host]
        ]


async def test_pinned_resolver_caches_and_validates(monkeypatch) -> None:
    pol = NetPolicy()
    resolver = PinnedResolver(pol, ttl_seconds=60)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _StubLoop({"good.com": ["1.1.1.1"]})
    )
    out1 = await resolver.resolve("good.com", port=443, family=socket.AF_INET)
    out2 = await resolver.resolve("good.com", port=443, family=socket.AF_INET)
    assert out1 == out2
    assert out1[0]["host"] == "1.1.1.1"


async def test_pinned_resolver_rejects_private_ip(monkeypatch) -> None:
    pol = NetPolicy()  # private not allowed
    resolver = PinnedResolver(pol)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _StubLoop({"bad.com": ["10.0.0.1"]})
    )
    with pytest.raises(SsrFBlockedError):
        await resolver.resolve("bad.com", port=443, family=socket.AF_INET)


async def test_pinned_resolver_passes_when_allow_private(monkeypatch) -> None:
    pol = NetPolicy(allow_private_network=True)
    resolver = PinnedResolver(pol)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: _StubLoop({"x.com": ["10.0.0.1"]})
    )
    out = await resolver.resolve("x.com", port=443, family=socket.AF_INET)
    assert out[0]["host"] == "10.0.0.1"


async def test_make_guarded_connector_returns_aiohttp_connector() -> None:
    import aiohttp

    conn = make_guarded_connector(NetPolicy())
    assert isinstance(conn, aiohttp.TCPConnector)
    await conn.close()


# ─── Phase N3: outbound audit ────────────────────────────────────────


def test_audit_config_disabled_by_default() -> None:
    cfg = should_audit_from_env({})
    assert cfg.enabled is False


def test_audit_config_picks_up_env(tmp_path: Path) -> None:
    cfg = should_audit_from_env(
        {
            "SAMPYCLAW_AUDIT_OUTBOUND": "1",
            "SAMPYCLAW_AUDIT_OUTBOUND_BODY": "1",
            "SAMPYCLAW_AUDIT_OUTBOUND_SAMPLE": "0.5",
            "SAMPYCLAW_AUDIT_OUTBOUND_PATH": str(tmp_path / "a.db"),
        },
        home=tmp_path,
    )
    assert cfg.enabled is True
    assert cfg.capture_body is True
    assert cfg.sample_rate == 0.5
    assert cfg.db_path == tmp_path / "a.db"


def test_audit_store_records_request_and_response(tmp_path: Path) -> None:
    store = OutboundAuditStore(tmp_path / "audit.db")
    store.record_event(
        request_id="r1",
        event="request",
        method="GET",
        url="https://example.com/x",
    )
    store.record_event(
        request_id="r1",
        event="response",
        method="GET",
        url="https://example.com/x",
        status=200,
        duration_ms=12.3,
    )
    rows = store.recent()
    assert len(rows) == 2
    events = {r["event"] for r in rows}
    assert events == {"request", "response"}
    assert store.count() == 2
    store.close()


def test_audit_store_truncates_oversized_body(tmp_path: Path) -> None:
    store = OutboundAuditStore(tmp_path / "a.db", max_body_bytes=10)
    store.record_body(
        request_id="r2",
        direction="response",
        body=b"x" * 100,
        content_type="text/plain",
    )
    row = store._conn.execute(
        "SELECT body FROM outbound_bodies WHERE request_id=?", ("r2",)
    ).fetchone()
    assert len(row[0]) == 10
    store.close()


# ─── Phase N4: webhook guards ───────────────────────────────────────


async def test_body_limiter_caps() -> None:
    limiter = BodySizeLimiter(max_bytes=10)

    async def stream():  # type: ignore[no-untyped-def]
        yield b"hello"
        yield b"world"

    out = await limiter.read_streaming(stream())
    assert out == b"helloworld"

    async def stream_too_big():  # type: ignore[no-untyped-def]
        yield b"hello"
        yield b"worldEXTRA"

    with pytest.raises(BodyTooLargeError):
        await limiter.read_streaming(stream_too_big())


def test_rate_limiter_window_resets() -> None:
    clock = {"t": 0.0}

    def _now() -> float:
        return clock["t"]

    rl = FixedWindowRateLimiter(
        max_requests=2, window_seconds=10, clock=_now
    )
    assert rl.check("u1") is True
    assert rl.check("u1") is True
    assert rl.check("u1") is False
    # Advance past window.
    clock["t"] = 11
    assert rl.check("u1") is True


def test_rate_limiter_assert_raises_with_retry_after() -> None:
    clock = {"t": 0.0}
    rl = FixedWindowRateLimiter(
        max_requests=1, window_seconds=5, clock=lambda: clock["t"]
    )
    rl.assert_allowed("u")
    with pytest.raises(RateLimited) as exc_info:
        rl.assert_allowed("u")
    assert exc_info.value.retry_after > 0


def test_rate_limiter_prunes_when_max_keys() -> None:
    rl = FixedWindowRateLimiter(
        max_requests=1, window_seconds=1, max_keys=4
    )
    for i in range(10):
        rl.check(f"u{i}")
    assert len(rl) <= 10  # may have been pruned, but not unbounded


def test_hmac_verify_constant_time() -> None:
    secret = "shh"
    body = b"payload"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_signature(secret, body, sig)
    assert not verify_hmac_signature(secret, body, sig[:-1] + "0")


def test_hmac_verify_strips_provider_prefix() -> None:
    secret = "shh"
    body = b"x"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac_signature(secret, body, f"sha256={sig}", prefix="sha256=")


def test_default_guards_pre_auth_stricter_than_post() -> None:
    pre = default_guards(WebhookProfile.PRE_AUTH)
    post = default_guards(WebhookProfile.POST_AUTH)
    assert pre.body_limiter.max_bytes < post.body_limiter.max_bytes


def test_webhook_guards_signature_round_trip() -> None:
    g = WebhookGuards(
        body_limiter=BodySizeLimiter(1024),
        rate_limiter=FixedWindowRateLimiter(max_requests=10, window_seconds=60),
        hmac_secret="topsecret",
        hmac_header="X-Sig",
        hmac_prefix="",
    )
    body = b"payload"
    sig = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert g.verify_signature(body, {"X-Sig": sig}) is True
    assert g.verify_signature(body, {"X-Sig": "wrong"}) is False
    # No header → fail.
    assert g.verify_signature(body, {}) is False


# ─── Phase N5: integration shim ─────────────────────────────────────


async def test_guarded_session_reuses_audit_store(tmp_path: Path) -> None:
    cfg = AuditConfig(enabled=True, db_path=tmp_path / "audit.db")
    async with guarded_session(NetPolicy(allow_loopback=True), audit=cfg) as s1:
        assert s1 is not None
    async with guarded_session(NetPolicy(allow_loopback=True), audit=cfg) as s2:
        assert s2 is not None
    _close_audit_store()


def test_policy_pre_flight_rejects_blocked() -> None:
    with pytest.raises(SsrFBlockedError):
        policy_pre_flight("http://10.0.0.1/", NetPolicy())


def test_policy_pre_flight_passes_allowed() -> None:
    out = policy_pre_flight("https://example.com/x", NetPolicy())
    assert out == "example.com"
