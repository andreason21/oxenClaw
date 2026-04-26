"""Multi-account Slack channel registry.

Constructs one SlackChannel per account declared in config, resolving
tokens via SlackTokenResolver. Mirrors the Telegram pattern so the
plugin loader behaviour is uniform.

Each `accounts[].extra` may carry:
- `base_url`: corp-internal Slack proxy override (default `https://slack.com/api`).
"""

from __future__ import annotations

from sampyclaw.config.credentials import CredentialStore
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.extensions.slack.channel import SlackChannel
from sampyclaw.extensions.slack.client import DEFAULT_BASE_URL
from sampyclaw.extensions.slack.token import SlackTokenResolver
from sampyclaw.plugin_sdk.config_schema import RootConfig
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("extensions.slack.accounts")


class SlackAccountRegistry:
    def __init__(
        self,
        *,
        paths: SampyclawPaths | None = None,
        tokens: SlackTokenResolver | None = None,
    ) -> None:
        resolved_paths = paths or default_paths()
        store = CredentialStore(resolved_paths)
        self._tokens = tokens or SlackTokenResolver(store)
        self._channels: dict[str, SlackChannel] = {}

    def load_from_config(self, config: RootConfig) -> list[str]:
        slack_cfg = config.channels.get("slack")
        loaded: list[str] = []
        if slack_cfg is None:
            return loaded
        # Channel-level base_url (corp proxy) is the default for every
        # account; per-account override wins.
        global_base = getattr(slack_cfg, "base_url", None) or DEFAULT_BASE_URL
        if hasattr(slack_cfg, "model_extra") and isinstance(slack_cfg.model_extra, dict):
            global_base = slack_cfg.model_extra.get("base_url", global_base)
        for acct in slack_cfg.accounts:
            account_id = acct.account_id
            if account_id in self._channels:
                continue
            token = self._tokens.resolve(account_id)
            if token is None:
                logger.warning("slack account %r has no token; skipping", account_id)
                continue
            extra = getattr(acct, "model_extra", None) or {}
            base_url = extra.get("base_url") or global_base
            self._channels[account_id] = SlackChannel(
                token=token,
                account_id=account_id,
                base_url=base_url,
            )
            loaded.append(account_id)
        return loaded

    def register(self, channel: SlackChannel) -> None:
        if channel.id != "slack":
            raise ValueError(f"expected slack channel, got {channel.id!r}")
        if channel._account_id in self._channels:
            raise ValueError(f"account {channel._account_id!r} already registered")
        self._channels[channel._account_id] = channel

    def get(self, account_id: str) -> SlackChannel | None:
        return self._channels.get(account_id)

    def require(self, account_id: str) -> SlackChannel:
        ch = self._channels.get(account_id)
        if ch is None:
            raise UserVisibleError(f"slack account {account_id!r} not registered")
        return ch

    def ids(self) -> list[str]:
        return sorted(self._channels)

    async def aclose(self) -> None:
        for ch in self._channels.values():
            await ch.aclose()
        self._channels.clear()
