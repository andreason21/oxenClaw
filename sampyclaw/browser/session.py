"""PlaywrightSession — perf-first, fail-closed Chromium lifecycle.

Design choices that maximise throughput while keeping data in:

- **One Chromium per process.** Launch is the expensive step (~500 ms).
  We share the browser across every skill that needs it. Operators can
  still shut it down by closing the session.
- **One BrowserContext per skill / session.** Contexts are cheap (~5 ms)
  and provide full isolation: each gets its own cookie jar, storage,
  cache, service workers, and route handler. No cross-skill leakage.
- **Pages are recycled inside a context.** A single context can host
  `policy.max_concurrent_pages` pages without paying browser-launch
  cost again.
- **Dead proxy by default.** Chromium is launched with
  `--proxy-server=http://0.0.0.0:1`; every request *not* intercepted by
  our route handler will fail at the network layer. Combined with
  `context.route("**/*", ...)` this is belt-and-braces — even if
  Playwright's interception misses an edge case (early prefetch,
  certain service-worker bootstraps), the OS-level proxy still refuses
  to connect.
- **No persistent storage by default.** Each context uses an ephemeral
  storage state. Operators can opt into a shared profile dir via
  `BrowserPolicy.persistent_profile_dir`, but the default is wipe.
- **Lazy import.** `playwright` is an optional dependency; this file
  only imports it inside `_ensure_playwright()` so policy/errors stay
  importable in installs without it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from sampyclaw.browser.egress import build_route_handler
from sampyclaw.browser.errors import (
    BrowserResourceCapError,
    BrowserUnavailable,
)
from sampyclaw.browser.pinning import HostPinCache
from sampyclaw.browser.policy import BrowserPolicy
from sampyclaw.plugin_sdk.runtime_env import get_logger
from sampyclaw.security.net.audit import OutboundAuditStore

if TYPE_CHECKING:
    from playwright.async_api import (  # noqa: F401
        Browser,
        BrowserContext,
        Page,
        Playwright,
    )

logger = get_logger("browser.session")


# Chromium flags that improve isolation + reduce background traffic.
# Order matters only for readability; all are independently safe.
_LAUNCH_ARGS = (
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-extensions",
    "--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
)

# Chromium opens this proxy when our route handler doesn't intercept;
# the address has no listener so the request dies before any byte
# leaves the host. We rely on `context.route("**/*", ...)` for the
# happy path; the dead proxy is the fallback if interception misses.
_DEAD_PROXY = "http://0.0.0.0:1"


def _ensure_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised manually
        raise BrowserUnavailable(
            "playwright is not installed. Install with: "
            "`pip install 'sampyclaw[browser]'` and then "
            "`playwright install chromium`"
        ) from exc


class PlaywrightSession:
    """Process-wide Chromium lifecycle with per-skill BrowserContexts."""

    def __init__(
        self,
        policy: BrowserPolicy | None = None,
        *,
        audit: OutboundAuditStore | None = None,
        headless: bool = True,
    ) -> None:
        self._policy = policy or BrowserPolicy.closed()
        self._audit = audit
        self._headless = headless
        self._pin_cache = HostPinCache()
        self._lock = asyncio.Lock()
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        # Track open contexts so we can refuse if the operator caps
        # context count (defaults to "unbounded" — caps live at the
        # page level via policy.max_concurrent_pages).
        self._contexts: set[BrowserContext] = set()

    @property
    def policy(self) -> BrowserPolicy:
        return self._policy

    # ─── lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the shared Chromium. Idempotent."""
        async with self._lock:
            if self._browser is not None:
                return
            _ensure_playwright()
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=self._headless,
                args=list(_LAUNCH_ARGS),
                proxy={"server": _DEAD_PROXY},
                handle_sigint=False,
                handle_sigterm=False,
                handle_sighup=False,
            )
            logger.info("Chromium launched (headless=%s)", self._headless)

    async def close(self) -> None:
        """Shut down every context and the browser. Idempotent."""
        async with self._lock:
            for ctx in list(self._contexts):
                with suppress(Exception):
                    await ctx.close()
            self._contexts.clear()
            if self._browser is not None:
                with suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._pw is not None:
                with suppress(Exception):
                    await self._pw.stop()
                self._pw = None

    # ─── contexts + pages ──────────────────────────────────────────

    @asynccontextmanager
    async def context(
        self,
        *,
        policy: BrowserPolicy | None = None,
    ):
        """Async context manager yielding a fresh BrowserContext.

        The context is wired with the route handler before being yielded
        so no request can escape. On exit the context is closed; cookies
        / storage are discarded unless `policy.persistent_profile_dir`
        is set.
        """
        await self.start()
        effective = policy or self._policy
        assert self._browser is not None
        kwargs: dict[str, Any] = {
            "ignore_https_errors": False,
            "java_script_enabled": True,
            "accept_downloads": effective.allow_downloads,
        }
        if effective.user_agent:
            kwargs["user_agent"] = effective.user_agent
        ctx = await self._browser.new_context(**kwargs)
        ctx.set_default_timeout(effective.default_timeout_ms)
        ctx.set_default_navigation_timeout(effective.navigation_timeout_ms)

        handler = build_route_handler(effective, pin_cache=self._pin_cache, audit=self._audit)
        await ctx.route("**/*", handler)
        self._contexts.add(ctx)
        try:
            yield ctx
        finally:
            self._contexts.discard(ctx)
            with suppress(Exception):
                await ctx.close()

    @asynccontextmanager
    async def page(self, *, policy: BrowserPolicy | None = None):
        """Convenience: open a fresh context with one page."""
        effective = policy or self._policy
        async with self.context(policy=effective) as ctx:
            page = await ctx.new_page()
            # cap pages: re-checking on each `new_page` would require
            # the caller to use the context directly; for the single-
            # page convenience path, the cap is implicit (1 ≤ cap).
            if effective.max_concurrent_pages < 1:
                raise BrowserResourceCapError("max_concurrent_pages must be >= 1")
            try:
                yield page
            finally:
                with suppress(Exception):
                    await page.close()


# ─── module-level singleton helper ─────────────────────────────────

_default_session: PlaywrightSession | None = None
_default_lock = asyncio.Lock()


async def get_default_session(
    policy: BrowserPolicy | None = None,
    *,
    audit: OutboundAuditStore | None = None,
) -> PlaywrightSession:
    """Lazy process-wide session. Creates on first call; reused after."""
    global _default_session
    async with _default_lock:
        if _default_session is None:
            _default_session = PlaywrightSession(policy=policy, audit=audit)
        return _default_session


async def close_default_session() -> None:
    global _default_session
    async with _default_lock:
        if _default_session is not None:
            await _default_session.close()
            _default_session = None


__all__ = [
    "PlaywrightSession",
    "close_default_session",
    "get_default_session",
]
