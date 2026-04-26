"""Egress chokepoint for Playwright.

`page.route("**/*", handler)` (or `context.route(...)`) is the single
chokepoint Playwright exposes for **every** HTTP/HTTPS/WebSocket
request the browser makes. We install our handler at the
`BrowserContext` level so it covers the page itself, sub-resources,
fetches issued by JavaScript, service workers, and shared workers.

For each request:

1. URL pre-flight via `assert_url_allowed(req.url, policy.net)`.
   Catches scheme/port/hostname-pattern + IP-literal violations.
2. Resource-type filter — websockets / event-sources require
   `policy.allow_websockets`.
3. DNS pinning via `HostPinCache.resolve_or_pin(...)`. First-seen IPs
   are cached; subsequent disjoint IP sets are refused
   (`RebindBlockedError`). Each IP is independently re-validated
   against `NetPolicy`.
4. Audit-log the decision (when an `OutboundAuditStore` is provided).

On any failure we call `route.abort("blockedbyclient")` so Chromium
surfaces a clear `net::ERR_BLOCKED_BY_CLIENT` to the page; on success
we call `route.continue_()` with no overrides.

Performance: the handler does ~1 µs URL parse + ~1 µs cache hit on the
hot path. `OutboundAuditStore` writes are skipped when the store is
None, so the hot path is allocation-light.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from sampyclaw.browser.errors import RebindBlockedError
from sampyclaw.browser.pinning import HostPinCache
from sampyclaw.browser.policy import BrowserPolicy
from sampyclaw.plugin_sdk.runtime_env import get_logger
from sampyclaw.security.net.audit import OutboundAuditStore
from sampyclaw.security.net.ssrf import SsrFBlockedError, assert_url_allowed

logger = get_logger("browser.egress")


# Resource types Chromium reports through Playwright's Request API.
# See https://playwright.dev/python/docs/api/class-request#request-resource-type
_NETWORK_TYPES = {
    "document",
    "stylesheet",
    "image",
    "media",
    "font",
    "script",
    "texttrack",
    "xhr",
    "fetch",
    "manifest",
    "ping",
    "preflight",
    "other",
}
_REALTIME_TYPES = {"websocket", "eventsource"}


RouteHandler = Callable[[Any, Any], Awaitable[None]]


def build_route_handler(
    policy: BrowserPolicy,
    *,
    pin_cache: HostPinCache | None = None,
    audit: OutboundAuditStore | None = None,
) -> RouteHandler:
    """Return an async `(route, request)` handler ready for `context.route()`."""
    # NB: `pin_cache or HostPinCache()` is wrong because HostPinCache
    # defines __len__, so an empty cache is boolean-falsy and would be
    # silently replaced by a fresh default-TTL cache.
    pins = pin_cache if pin_cache is not None else HostPinCache()

    async def _handler(route: Any, request: Any) -> None:
        url = request.url
        method = request.method
        resource_type = request.resource_type
        request_id = uuid4().hex
        start = time.monotonic()

        def _audit(event: str, *, status: int | None = None, error: str | None = None) -> None:
            if audit is None:
                return
            try:
                audit.record_event(
                    request_id=request_id,
                    event=event,
                    method=method,
                    url=url,
                    status=status,
                    duration_ms=(time.monotonic() - start) * 1000.0,
                    error=error,
                )
            except Exception:
                logger.warning("audit write failed", exc_info=True)

        # 1. realtime gate
        if resource_type in _REALTIME_TYPES and not policy.allow_websockets:
            _audit("blocked", error=f"resource_type {resource_type!r} disallowed")
            await _abort(route, "blockedbyclient")
            return

        # 2. URL preflight
        try:
            host = assert_url_allowed(url, policy.net)
        except SsrFBlockedError as exc:
            _audit("blocked", error=str(exc))
            logger.info("browser request blocked: %s (%s)", url, exc)
            await _abort(route, "blockedbyclient")
            return

        # 3. DNS pinning + per-IP validation
        try:
            pins.resolve_or_pin(host, policy)
        except RebindBlockedError as exc:
            _audit("blocked", error=f"rebind: {exc}")
            logger.warning("rebind blocked: %s", exc)
            await _abort(route, "blockedbyclient")
            return
        except SsrFBlockedError as exc:
            _audit("blocked", error=str(exc))
            await _abort(route, "blockedbyclient")
            return

        _audit("request")
        try:
            await route.continue_()
        except Exception as exc:
            # Playwright raises when the request was already handled
            # or the page navigated away; benign in practice.
            logger.debug("route.continue_ failed for %s: %s", url, exc)

    return _handler


async def _abort(route: Any, reason: str) -> None:
    try:
        await route.abort(reason)
    except Exception:
        # Already-handled / page-closed; nothing to do.
        return


__all__ = ["RouteHandler", "build_route_handler"]
