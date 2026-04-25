"""Browser-subsystem error types."""

from __future__ import annotations


class BrowserUnavailable(RuntimeError):
    """Raised when `playwright` is not installed but a browser tool was invoked."""


class BrowserPolicyError(RuntimeError):
    """Raised when the requested browser action is refused by policy."""


class RebindBlockedError(BrowserPolicyError):
    """Raised when a host's resolved IP changed after pinning (DNS rebinding)."""


class BrowserResourceCapError(BrowserPolicyError):
    """Raised when a result exceeds a hard cap (page count, payload size)."""


__all__ = [
    "BrowserPolicyError",
    "BrowserResourceCapError",
    "BrowserUnavailable",
    "RebindBlockedError",
]
