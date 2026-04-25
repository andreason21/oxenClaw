"""Error taxonomy surfaced to plugins and the gateway.

Port of openclaw `src/plugin-sdk/error-runtime.ts`.
"""

from __future__ import annotations


class ChannelError(Exception):
    """Base class for all channel plugin errors."""


class UserVisibleError(ChannelError):
    """Error whose `message` is safe to forward to the end user on-channel."""


class NetworkError(ChannelError):
    """Transient network failure; caller may retry with backoff."""


class RateLimitedError(NetworkError):
    """Upstream signalled rate-limit (e.g. HTTP 429). `retry_after` is seconds."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
