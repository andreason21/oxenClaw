"""Channel-agnostic send/probe router.

Holds every loaded `(channel_id, account_id) → ChannelPlugin` binding. The
`Dispatcher` calls `ChannelRouter.send` without knowing which concrete
plugin owns the target; `channels.list` and `channels.probe` walk this
registry for inspection.
"""

from __future__ import annotations

from collections.abc import Iterator

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


class ChannelRouter:
    """Channel-id and account-id indexed registry of ChannelPlugin instances."""

    def __init__(self) -> None:
        self._by_binding: dict[tuple[str, str], ChannelPlugin] = {}

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
        for _, _, plugin in self.bindings():
            closer = getattr(plugin, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                logger.exception("plugin aclose raised")
        self._by_binding.clear()
