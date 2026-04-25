"""Generic `channel.monitor(opts)` restart-with-backoff runner.

Wraps the SDK `ChannelPlugin.monitor()` contract in a supervisor loop:
if `monitor()` returns cleanly or raises, the runner sleeps with
exponential backoff + jitter and reinvokes it. Shutdown is driven by
`stop()` + task cancellation — `monitor()` must respect CancelledError.

Generalises `sampyclaw.extensions.telegram.polling_runner.PollingRunner`
to any channel that implements the SDK contract.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from sampyclaw.plugin_sdk.channel_contract import MonitorOpts
from sampyclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from sampyclaw.plugin_sdk.channel_contract import ChannelPlugin

logger = get_logger("channels.runner")

# A monitor that ran cleanly for at least this long before crashing is
# treated as "previously stable" — backoff resets so a transient blip
# doesn't keep the channel in slow-restart mode forever.
DEFAULT_STABLE_RESET_SECONDS = 60.0


class ChannelRunner:
    def __init__(
        self,
        channel: ChannelPlugin,
        opts: MonitorOpts,
        *,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        jitter: float = 0.3,
        max_restarts: int | None = None,
        stable_reset_seconds: float = DEFAULT_STABLE_RESET_SECONDS,
        sleep=asyncio.sleep,  # type: ignore[no-untyped-def]
        clock=time.monotonic,  # type: ignore[no-untyped-def]
    ) -> None:
        if initial_backoff <= 0 or max_backoff < initial_backoff:
            raise ValueError("require 0 < initial_backoff <= max_backoff")
        if not 0 <= jitter < 1:
            raise ValueError("jitter must be in [0, 1)")
        if max_restarts is not None and max_restarts < 0:
            raise ValueError("max_restarts must be >= 0 or None")
        self._channel = channel
        self._opts = opts
        self._initial = initial_backoff
        self._max = max_backoff
        self._jitter = jitter
        self._max_restarts = max_restarts
        self._stable_reset = stable_reset_seconds
        self._sleep = sleep
        self._clock = clock
        self._stopped = False
        self.restart_count = 0
        self.gave_up = False

    @property
    def channel_id(self) -> str:
        return self._channel.id

    @property
    def account_id(self) -> str:
        return self._opts.account_id

    async def run_forever(self) -> None:
        backoff = self._initial
        while not self._stopped:
            started_at = self._clock()
            try:
                await self._channel.monitor(self._opts)
                if self._stopped:
                    return
                logger.warning(
                    "monitor %s:%s returned without stop; restarting in %.1fs",
                    self.channel_id,
                    self.account_id,
                    backoff,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "monitor %s:%s crashed; restarting in %.1fs",
                    self.channel_id,
                    self.account_id,
                    backoff,
                )
            ran_for = self._clock() - started_at
            if ran_for >= self._stable_reset:
                # Long-stable run before failure — reset the backoff so the
                # next restart is fast, not stuck at the previous max.
                backoff = self._initial
            if (
                self._max_restarts is not None
                and self.restart_count >= self._max_restarts
            ):
                logger.error(
                    "monitor %s:%s reached max_restarts=%d; giving up",
                    self.channel_id,
                    self.account_id,
                    self._max_restarts,
                )
                self.gave_up = True
                return
            # Symmetric jitter: uniform [backoff*(1-j), backoff*(1+j)].
            # Replaces the prior positive-only `backoff*(1+random[0,j])`
            # that was always-late and skewed group restarts. With j=0
            # this collapses to exactly `backoff`, preserving determinism
            # for tests and "no jitter" deployments.
            if self._jitter == 0:
                wait = backoff
            else:
                wait = backoff * (1.0 + random.uniform(-self._jitter, self._jitter))
            await self._sleep(max(0.0, wait))
            self.restart_count += 1
            backoff = min(backoff * 2, self._max)

    async def stop(self) -> None:
        """Prevent further restarts. Caller is expected to cancel the task so
        `monitor()` actually returns."""
        self._stopped = True
