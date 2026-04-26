"""Classify aiogram/Telegram exceptions into SDK error categories.

Port of openclaw `extensions/telegram/src/network-errors.ts`. Kept separate
from `send.py` so the polling runner can reuse the same taxonomy.
"""

from __future__ import annotations

from oxenclaw.plugin_sdk.error_runtime import (
    ChannelError,
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)


def classify(exc: BaseException) -> ChannelError:
    """Wrap a raw exception into the SDK error taxonomy. Returns a new error instance."""
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNetworkError,
        TelegramRetryAfter,
    )

    if isinstance(exc, TelegramRetryAfter):
        return RateLimitedError(str(exc), retry_after=float(exc.retry_after))
    if isinstance(exc, TelegramNetworkError):
        return NetworkError(str(exc))
    if isinstance(exc, (TelegramBadRequest, TelegramForbiddenError)):
        return UserVisibleError(str(exc))
    return ChannelError(str(exc))


def is_retryable(exc: BaseException) -> bool:
    """Polling runner asks: should we back off and retry after this error?"""
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNetworkError,
        TelegramRetryAfter,
    )

    if isinstance(exc, (TelegramNetworkError, TelegramRetryAfter)):
        return True
    if isinstance(exc, (TelegramBadRequest, TelegramForbiddenError)):
        return False
    return True  # default: unknown errors are retryable (caller decides on ceilings)
