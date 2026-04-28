"""Channel-agnostic send/probe router.

Holds every loaded `(channel_id, account_id) → ChannelPlugin` binding. The
`Dispatcher` calls `ChannelRouter.send` without knowing which concrete
plugin owns the target; `channels.list` and `channels.probe` walk this
registry for inspection.

Reconnect watcher. When a channel goes unhealthy (probe failure /
explicit `mark_failed`) we record it in `_failed_channels` and start a
background retry loop with exponential backoff (30 → 60 → 120 → 240 →
300 s, max 20 attempts). Auth-shaped errors stop retrying immediately
because re-attempting a known-bad token just compounds the problem.
The dashboard surfaces the reconnect state via `channels.health`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from oxenclaw.plugin_sdk.channel_contract import (
    ChannelPlugin,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("channels.router")


_RECONNECT_BACKOFF_S: tuple[int, ...] = (30, 60, 120, 240, 300)
_MAX_RECONNECT_ATTEMPTS = 20


def is_auth_error(message: str | None) -> bool:
    """Heuristic: True iff `message` looks like a credential failure."""
    if not message:
        return False
    text = message.lower()
    return any(token in text for token in ("401", "unauthor", "forbidden", "403"))


@dataclass
class FailedChannelState:
    attempts: int = 0
    next_retry: float = 0.0
    last_error: str = ""
    auth_error: bool = False
    last_attempt: float = 0.0


class ChannelRouter:
    """Channel-id and account-id indexed registry of ChannelPlugin instances."""

    def __init__(self) -> None:
        self._by_binding: dict[tuple[str, str], ChannelPlugin] = {}
        # Reconnect watcher state. Keys are `(channel_id, account_id)`.
        self._failed_channels: dict[tuple[str, str], FailedChannelState] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_stop: asyncio.Event | None = None

    def register(self, channel_id: str, account_id: str, plugin: ChannelPlugin) -> None:
        key = (channel_id, account_id)
        if key in self._by_binding:
            raise ValueError(f"channel {channel_id!r} already has account {account_id!r}")
        self._by_binding[key] = plugin

    def get(self, channel_id: str, account_id: str) -> ChannelPlugin | None:
        return self._by_binding.get((channel_id, account_id))

    def require(self, channel_id: str, account_id: str) -> ChannelPlugin:
        plugin = self.get(channel_id, account_id)
        if plugin is None:
            raise UserVisibleError(f"no channel plugin for {channel_id}:{account_id}")
        return plugin

    def channels_by_id(self) -> dict[str, list[str]]:
        """Group by channel_id → sorted list of account_ids."""
        out: dict[str, list[str]] = {}
        for channel_id, account_id in self._by_binding:
            out.setdefault(channel_id, []).append(account_id)
        return {k: sorted(v) for k, v in out.items()}

    def bindings(self) -> Iterator[tuple[str, str, ChannelPlugin]]:
        for (channel_id, account_id), plugin in self._by_binding.items():
            yield channel_id, account_id, plugin

    def __len__(self) -> int:
        return len(self._by_binding)

    async def send(self, params: SendParams) -> SendResult:
        plugin = self.get(params.target.channel, params.target.account_id)
        if plugin is None:
            # Surface routing misconfig to the caller instead of returning a
            # synthetic ok-looking SendResult that masks the failure (the prior
            # behavior). Callers / RPC clients see an explicit error.
            raise UserVisibleError(
                f"no channel plugin for {params.target.channel}:{params.target.account_id}"
            )
        return await plugin.send(params)

    async def probe(self, channel_id: str, account_id: str) -> ProbeResult:
        plugin = self.get(channel_id, account_id)
        if plugin is None:
            return ProbeResult(
                ok=False,
                account_id=account_id,
                error=f"channel {channel_id!r} account {account_id!r} not loaded",
            )
        return await plugin.probe(ProbeOpts(account_id=account_id))

    async def aclose(self) -> None:
        await self.stop_reconnect_watcher()
        for _, _, plugin in self.bindings():
            closer = getattr(plugin, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                logger.exception("plugin aclose raised")
        self._by_binding.clear()

    # ------------------------------------------------------------------
    # Reconnect watcher
    # ------------------------------------------------------------------

    def mark_failed(
        self,
        channel_id: str,
        account_id: str,
        *,
        error: str = "",
    ) -> FailedChannelState:
        """Record a channel failure; schedule a retry per backoff ladder."""
        key = (channel_id, account_id)
        state = self._failed_channels.get(key) or FailedChannelState()
        state.attempts += 1
        state.last_error = error
        state.last_attempt = time.time()
        if is_auth_error(error):
            # Auth errors stop the retry loop — re-attempting a known
            # bad credential just compounds the rate-limit damage.
            state.auth_error = True
            state.next_retry = 0.0
        else:
            idx = min(state.attempts - 1, len(_RECONNECT_BACKOFF_S) - 1)
            state.next_retry = time.time() + _RECONNECT_BACKOFF_S[idx]
        self._failed_channels[key] = state
        logger.info(
            "channel %s:%s marked failed (attempt=%d auth=%s next_retry=%.0fs)",
            channel_id,
            account_id,
            state.attempts,
            state.auth_error,
            max(0.0, state.next_retry - time.time()),
        )
        return state

    def mark_recovered(self, channel_id: str, account_id: str) -> None:
        """Drop a channel from the failed list after a successful probe."""
        self._failed_channels.pop((channel_id, account_id), None)

    def health(self) -> dict[str, Any]:
        """Render the reconnect watcher state for `channels.health` RPC."""
        now = time.time()
        bindings_count = len(self._by_binding)
        failed: list[dict[str, Any]] = []
        for (cid, aid), state in self._failed_channels.items():
            failed.append(
                {
                    "channel_id": cid,
                    "account_id": aid,
                    "attempts": state.attempts,
                    "last_error": state.last_error,
                    "auth_error": state.auth_error,
                    "next_retry_in_s": max(0.0, state.next_retry - now)
                    if state.next_retry > 0
                    else None,
                }
            )
        return {
            "bindings": bindings_count,
            "failed": failed,
            "watcher_running": self._watcher_task is not None and not self._watcher_task.done(),
        }

    async def start_reconnect_watcher(self) -> None:
        """Start the background retry loop. Idempotent."""
        if self._watcher_task is not None and not self._watcher_task.done():
            return
        self._watcher_stop = asyncio.Event()
        self._watcher_task = asyncio.create_task(self._reconnect_loop())

    async def stop_reconnect_watcher(self) -> None:
        if self._watcher_stop is not None:
            self._watcher_stop.set()
        task = self._watcher_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except TimeoutError:
                task.cancel()
            except Exception:
                pass
        self._watcher_task = None
        self._watcher_stop = None

    async def _reconnect_loop(self) -> None:
        assert self._watcher_stop is not None
        while not self._watcher_stop.is_set():
            await self._tick_reconnect_watcher()
            try:
                await asyncio.wait_for(self._watcher_stop.wait(), timeout=5.0)
                # If we get here, stop was signalled.
                return
            except TimeoutError:
                continue

    async def _tick_reconnect_watcher(self) -> None:
        """One pass over `_failed_channels` — retry whatever is due."""
        now = time.time()
        due: list[tuple[tuple[str, str], FailedChannelState]] = []
        for key, state in list(self._failed_channels.items()):
            if state.auth_error:
                continue  # never auto-retry auth errors
            if state.attempts >= _MAX_RECONNECT_ATTEMPTS:
                continue
            if state.next_retry <= now:
                due.append((key, state))

        for (cid, aid), _state in due:
            try:
                result = await self.probe(cid, aid)
            except Exception as exc:
                self.mark_failed(cid, aid, error=str(exc))
                continue
            if getattr(result, "ok", False):
                logger.info("channel %s:%s recovered", cid, aid)
                self.mark_recovered(cid, aid)
            else:
                self.mark_failed(cid, aid, error=getattr(result, "error", "") or "")
