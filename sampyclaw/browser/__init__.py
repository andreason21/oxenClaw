"""Browser subsystem — Playwright-backed, fail-closed egress.

Public re-exports keep the import surface small and stable; concrete
classes (PlaywrightSession, route handlers) live in submodules so that
`policy` + `errors` stay importable without `playwright` installed.
"""

from sampyclaw.browser.errors import (
    BrowserPolicyError,
    BrowserResourceCapError,
    BrowserUnavailable,
    RebindBlockedError,
)
from sampyclaw.browser.policy import BrowserPolicy, merge_browser_policies

__all__ = [
    "BrowserPolicy",
    "BrowserPolicyError",
    "BrowserResourceCapError",
    "BrowserUnavailable",
    "RebindBlockedError",
    "merge_browser_policies",
]
