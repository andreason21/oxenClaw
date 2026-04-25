"""BrowserPolicy — NetPolicy + browser-only resource caps.

Composition: a `NetPolicy` (reused from `security.net`) governs every
HTTP/WS request the browser makes; `BrowserPolicy` adds caps that only
make sense at the browser layer (page count, screenshot size, evaluate
output size, persistent profile path, etc.).

Defaults are **fully closed**: `https`-only, no loopback, no private
network, empty hostname allowlist (so the policy refuses every
navigation until the operator opts in for a skill or session).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from sampyclaw.security.net.policy import NetPolicy, merge_policies

# Hard ceilings that even an "open" policy will not exceed; protect us
# from an LLM choosing absurd values when a skill exposes the cap.
ABSOLUTE_MAX_PAGES = 16
ABSOLUTE_MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
ABSOLUTE_MAX_EVAL_CHARS = 200_000
ABSOLUTE_MAX_DOM_CHARS = 500_000
ABSOLUTE_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class BrowserPolicy:
    """Combined net policy + browser resource caps."""

    net: NetPolicy = field(default_factory=NetPolicy)
    max_concurrent_pages: int = 4
    default_timeout_ms: int = 15_000
    navigation_timeout_ms: int = 20_000
    max_screenshot_bytes: int = 2 * 1024 * 1024
    max_eval_chars: int = 20_000
    max_dom_chars: int = 80_000
    max_download_bytes: int = 8 * 1024 * 1024
    allow_downloads: bool = False
    allow_websockets: bool = False
    persistent_profile_dir: Path | None = None
    user_agent: str | None = None

    def __post_init__(self) -> None:
        if self.max_concurrent_pages < 1:
            raise ValueError("max_concurrent_pages must be >= 1")
        if self.max_concurrent_pages > ABSOLUTE_MAX_PAGES:
            raise ValueError(
                f"max_concurrent_pages exceeds hard cap "
                f"({self.max_concurrent_pages} > {ABSOLUTE_MAX_PAGES})"
            )
        if self.max_screenshot_bytes > ABSOLUTE_MAX_SCREENSHOT_BYTES:
            raise ValueError("max_screenshot_bytes exceeds hard cap")
        if self.max_eval_chars > ABSOLUTE_MAX_EVAL_CHARS:
            raise ValueError("max_eval_chars exceeds hard cap")
        if self.max_dom_chars > ABSOLUTE_MAX_DOM_CHARS:
            raise ValueError("max_dom_chars exceeds hard cap")
        if self.max_download_bytes > ABSOLUTE_MAX_DOWNLOAD_BYTES:
            raise ValueError("max_download_bytes exceeds hard cap")

    @classmethod
    def closed(cls) -> BrowserPolicy:
        """The strictest sensible default — refuses everything until extended."""
        return cls(
            net=NetPolicy(
                allowed_hostnames=(),
                denied_hostnames=(),
                allow_private_network=False,
                allow_loopback=False,
                allowed_schemes=("https",),
            )
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BrowserPolicy:
        """Build from `SAMPYCLAW_BROWSER_*` env vars (used by gateway boot)."""
        src = env if env is not None else os.environ
        from sampyclaw.security.net.policy import policy_from_env

        net = policy_from_env(src)
        kwargs: dict = {"net": net}
        if "SAMPYCLAW_BROWSER_MAX_PAGES" in src:
            kwargs["max_concurrent_pages"] = int(src["SAMPYCLAW_BROWSER_MAX_PAGES"])
        if "SAMPYCLAW_BROWSER_TIMEOUT_MS" in src:
            kwargs["default_timeout_ms"] = int(src["SAMPYCLAW_BROWSER_TIMEOUT_MS"])
        if src.get("SAMPYCLAW_BROWSER_ALLOW_DOWNLOADS", "").lower() in ("1", "true", "yes"):
            kwargs["allow_downloads"] = True
        if src.get("SAMPYCLAW_BROWSER_ALLOW_WS", "").lower() in ("1", "true", "yes"):
            kwargs["allow_websockets"] = True
        if "SAMPYCLAW_BROWSER_PROFILE_DIR" in src:
            kwargs["persistent_profile_dir"] = Path(src["SAMPYCLAW_BROWSER_PROFILE_DIR"])
        if "SAMPYCLAW_BROWSER_USER_AGENT" in src:
            kwargs["user_agent"] = src["SAMPYCLAW_BROWSER_USER_AGENT"]
        return cls(**kwargs)

    def with_extra_allowed_hosts(self, *hosts: str) -> BrowserPolicy:
        return replace(self, net=self.net.with_extra_allow(*hosts))


def merge_browser_policies(*policies: BrowserPolicy | None) -> BrowserPolicy:
    """Restrictive merge: numeric caps take the **min**, flags AND, net merges."""
    real = [p for p in policies if p is not None]
    if not real:
        return BrowserPolicy()
    if len(real) == 1:
        return real[0]
    out = real[0]
    for p in real[1:]:
        out = BrowserPolicy(
            net=merge_policies(out.net, p.net),
            max_concurrent_pages=min(out.max_concurrent_pages, p.max_concurrent_pages),
            default_timeout_ms=min(out.default_timeout_ms, p.default_timeout_ms),
            navigation_timeout_ms=min(out.navigation_timeout_ms, p.navigation_timeout_ms),
            max_screenshot_bytes=min(out.max_screenshot_bytes, p.max_screenshot_bytes),
            max_eval_chars=min(out.max_eval_chars, p.max_eval_chars),
            max_dom_chars=min(out.max_dom_chars, p.max_dom_chars),
            max_download_bytes=min(out.max_download_bytes, p.max_download_bytes),
            allow_downloads=out.allow_downloads and p.allow_downloads,
            allow_websockets=out.allow_websockets and p.allow_websockets,
            persistent_profile_dir=out.persistent_profile_dir or p.persistent_profile_dir,
            user_agent=out.user_agent or p.user_agent,
        )
    return out


__all__ = [
    "ABSOLUTE_MAX_DOM_CHARS",
    "ABSOLUTE_MAX_DOWNLOAD_BYTES",
    "ABSOLUTE_MAX_EVAL_CHARS",
    "ABSOLUTE_MAX_PAGES",
    "ABSOLUTE_MAX_SCREENSHOT_BYTES",
    "BrowserPolicy",
    "merge_browser_policies",
]
