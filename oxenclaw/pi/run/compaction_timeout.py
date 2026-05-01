"""Aggregate wall-clock timeout shield for compaction calls.

Mirrors openclaw `pi-embedded-runner/run/compaction-retry-aggregate-timeout.ts`.
A stuck summariser (auxiliary LLM hung mid-stream, network black hole)
should not park the entire run forever — preemptive compaction can
trim history on the next attempt, but only if we let go of the stuck
call first.

The helper wraps a coroutine in `asyncio.wait_for`. On timeout it
swallows the `TimeoutError` (callers expect a non-fatal trim, not a
turn-failing exception) and notifies an optional callback so the
caller can log or telemetry the event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.compaction_timeout")

T = TypeVar("T")


async def with_compaction_timeout(
    coro: Awaitable[T],
    *,
    timeout_seconds: float | None,
    label: str = "compaction",
    on_timeout: Callable[[], None] | None = None,
) -> T | None:
    """Run `coro` under an aggregate timeout. Returns its result or None.

    `timeout_seconds=None` or `<= 0` disables the shield (legacy
    behaviour). The function never re-raises `TimeoutError` — callers
    rely on a non-fatal trim, so we log + return None instead.

    Cancellation propagation: when `wait_for` cancels the wrapped
    coroutine, asyncio runs its cleanup. The summariser thread/socket
    is the caller's responsibility — this shield only reclaims the
    Python coroutine slot.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError:
        logger.warning(
            "%s aggregate timeout after %.1fs — returning None and continuing",
            label,
            timeout_seconds,
        )
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception:
                logger.exception("%s on_timeout callback raised", label)
        return None


__all__ = ["with_compaction_timeout"]
