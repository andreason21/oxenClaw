"""Unit tests for oxenclaw.browser.egress.build_route_handler.

These tests use a hand-rolled Request / Route stub instead of spinning
up Playwright — the handler API surface is small (`request.url`,
`request.method`, `request.resource_type`, `route.continue_()`,
`route.abort()`).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any

import pytest

from oxenclaw.browser.egress import build_route_handler
from oxenclaw.browser.pinning import HostPinCache
from oxenclaw.browser.policy import BrowserPolicy
from oxenclaw.security.net.policy import NetPolicy


@dataclass
class _StubRequest:
    url: str
    method: str = "GET"
    resource_type: str = "document"


@dataclass
class _StubRoute:
    continued: int = 0
    aborted: list[str] = field(default_factory=list)

    async def continue_(self) -> None:
        self.continued += 1

    async def abort(self, reason: str = "failed") -> None:
        self.aborted.append(reason)


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    def fake(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        ips = mapping.get(host, [])
        if not ips:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]

    monkeypatch.setattr("oxenclaw.browser.pinning.socket.getaddrinfo", fake)


@pytest.mark.asyncio
async def test_allowed_url_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    pol = BrowserPolicy(net=NetPolicy(allowed_hostnames=("example.com",)))
    handler = build_route_handler(pol, pin_cache=HostPinCache())
    route = _StubRoute()
    await handler(route, _StubRequest(url="https://example.com/foo"))
    assert route.continued == 1
    assert route.aborted == []


@pytest.mark.asyncio
async def test_disallowed_host_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {})
    pol = BrowserPolicy(net=NetPolicy(allowed_hostnames=("example.com",)))
    handler = build_route_handler(pol, pin_cache=HostPinCache())
    route = _StubRoute()
    await handler(route, _StubRequest(url="https://evil.test/"))
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]


@pytest.mark.asyncio
async def test_loopback_literal_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {})
    pol = BrowserPolicy(net=NetPolicy(allowed_hostnames=()))  # permissive on names
    handler = build_route_handler(pol, pin_cache=HostPinCache())
    route = _StubRoute()
    await handler(route, _StubRequest(url="http://127.0.0.1/"))
    assert route.aborted == ["blockedbyclient"]


@pytest.mark.asyncio
async def test_websocket_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    pol = BrowserPolicy(net=NetPolicy(allowed_hostnames=("example.com",)))
    handler = build_route_handler(pol, pin_cache=HostPinCache())
    route = _StubRoute()
    await handler(
        route,
        _StubRequest(url="https://example.com/ws", resource_type="websocket"),
    )
    assert route.aborted == ["blockedbyclient"]


@pytest.mark.asyncio
async def test_websocket_allowed_when_policy_permits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    pol = BrowserPolicy(
        net=NetPolicy(allowed_hostnames=("example.com",)),
        allow_websockets=True,
    )
    handler = build_route_handler(pol, pin_cache=HostPinCache())
    route = _StubRoute()
    await handler(
        route,
        _StubRequest(url="https://example.com/ws", resource_type="websocket"),
    )
    assert route.continued == 1


@pytest.mark.asyncio
async def test_disallowed_scheme_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    pol = BrowserPolicy.closed()  # https-only
    handler = build_route_handler(pol)
    route = _StubRoute()
    await handler(route, _StubRequest(url="http://example.com/"))
    assert route.aborted == ["blockedbyclient"]


@pytest.mark.asyncio
async def test_pin_rebind_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    pol = BrowserPolicy(net=NetPolicy(allowed_hostnames=("example.com",)))
    cache = HostPinCache(ttl_seconds=0)
    handler = build_route_handler(pol, pin_cache=cache)

    _patch_resolver(monkeypatch, {"example.com": ["1.1.1.1"]})
    route1 = _StubRoute()
    await handler(route1, _StubRequest(url="https://example.com/a"))
    assert route1.continued == 1

    _patch_resolver(monkeypatch, {"example.com": ["2.2.2.2"]})
    route2 = _StubRoute()
    await handler(route2, _StubRequest(url="https://example.com/b"))
    assert route2.aborted == ["blockedbyclient"]
