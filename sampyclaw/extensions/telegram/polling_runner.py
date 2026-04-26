"""Exponential-backoff restart wrapper for TelegramPollingSession.

aiogram's internal Dispatcher retries individual getUpdates calls, but when
the dispatcher itself exits (on an unhandled error, or cleanly without a
stop request) we want to restart it with backoff. Port of the restart loop
in openclaw `extensions/telegram/src/polling-session.ts`.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from sampyclaw.extensions.telegram.network_errors import is_retryable
from sampyclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from sampyclaw.extensions.telegram.monitor import TelegramPollingSession

logger = get_logger("extensions.telegram.polling_runner")


class PollingRunner:
    """Runs a polling session forever, restarting with exponential backoff on error."""

    def __init__(
        self,
        session: TelegramPollingSession,
        *,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        jitter: float = 0.3,
        sleep=asyncio.sleep,  # type: ignore[no-untyped-def]
    ) -> None:
        if initial_backoff <= 0 or max_backoff < initial_backoff:
            raise ValueError("require 0 < initial_backoff <= max_backoff")
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be in [0, 1)")
        self._session = session
        self._initial = initial_backoff
        self._max = max_backoff
        self._jitter = jitter
        self._sleep = sleep
        self._stopped = False
        self.restart_count = 0

    async def run_forever(self) -> None:
        backoff = self._initial
        while not self._stopped:
            try:
                await self._session.start()
                if self._stopped:
                    return
                logger.warning(
                    "telegram polling returned without stop; restarting in %.1fs", backoff
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not is_retryable(exc):
                    logger.error("non-retryable polling error: %s", exc)
                    raise
                logger.exception("polling crashed; restarting in %.1fs", backoff)

            await self._sleep(backoff * (1 + random.uniform(0, self._jitter)))
            self.restart_count += 1
            backoff = min(backoff * 2, self._max)

    async def stop(self) -> None:
        self._stopped = True
        await self._session.stop()
