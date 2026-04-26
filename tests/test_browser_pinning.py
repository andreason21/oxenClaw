"""Unit tests for oxenclaw.browser.pinning.HostPinCache."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from oxenclaw.browser.errors import RebindBlockedError
from oxenclaw.browser.pinning import HostPinCache
from oxenclaw.browser.policy import BrowserPolicy
from oxenclaw.security.net.policy import NetPolicy
from oxenclaw.security.net.ssrf import SsrFBlockedError


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    """Stub socket.getaddrinfo so tests don't depend on real DNS."""
    calls: dict[str, int] = {}

    def fake(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        calls[host] = calls.get(host, 0) + 1
        ips = mapping.get(host, [])
        if not ips:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr("oxenclaw.browser.pinning.socket.getaddrinfo", fake)
    return calls  # type: ignore[return-value]


def _open_policy() -> BrowserPolicy:
    return BrowserPolicy(net=NetPolicy(allowed_hostnames=("*.example.com", "example.com")))


def test_first_resolve_caches_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    cache = HostPinCache()
    pol = _open_policy()
    ips = cache.resolve_or_pin("example.com", pol)
    assert ips == frozenset({"93.184.216.34"})
    assert len(cache) == 1


def test_second_resolve_hits_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_resolver(monkeypatch, {"example.com": ["1.1.1.1"]})
    cache = HostPinCache()
    pol = _open_policy()
    cache.resolve_or_pin("example.com", pol)
    cache.resolve_or_pin("example.com", pol)
    assert calls["example.com"] == 1


def test_rebind_to_disjoint_set_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    pol = _open_policy()
    cache = HostPinCache(ttl_seconds=0)  # always expire => always re-resolve
    _patch_resolver(monkeypatch, {"example.com": ["1.1.1.1"]})
    cache.resolve_or_pin("example.com", pol)
    _patch_resolver(monkeypatch, {"example.com": ["2.2.2.2"]})
    with pytest.raises(RebindBlockedError):
        cache.resolve_or_pin("example.com", pol)


def test_rebind_with_overlap_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    pol = _open_policy()
    cache = HostPinCache(ttl_seconds=0)
    _patch_resolver(monkeypatch, {"example.com": ["1.1.1.1"]})
    cache.resolve_or_pin("example.com", pol)
    _patch_resolver(monkeypatch, {"example.com": ["1.1.1.1", "1.1.1.2"]})
    merged = cache.resolve_or_pin("example.com", pol)
    assert merged == frozenset({"1.1.1.1", "1.1.1.2"})


def test_blocked_ip_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["127.0.0.1"]})
    cache = HostPinCache()
    pol = _open_policy()  # allow_loopback=False
    with pytest.raises(SsrFBlockedError):
        cache.resolve_or_pin("example.com", pol)


def test_invalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["1.1.1.1"]})
    cache = HostPinCache()
    pol = _open_policy()
    cache.resolve_or_pin("example.com", pol)
    cache.invalidate("example.com")
    assert len(cache) == 0


def test_capacity_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(
        monkeypatch,
        {f"h{i}.example.com": [f"1.1.1.{i}"] for i in range(5)},
    )
    cache = HostPinCache(capacity=3)
    pol = BrowserPolicy(net=NetPolicy(allowed_hostnames=("*.example.com",)))
    for i in range(5):
        cache.resolve_or_pin(f"h{i}.example.com", pol)
    assert len(cache) == 3
