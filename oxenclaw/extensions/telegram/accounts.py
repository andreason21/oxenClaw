"""Multi-account Telegram channel registry.

Constructs one TelegramChannel per account declared in config, resolving
tokens via TokenResolver. Callers fan send/monitor/probe calls out to the
right account through this registry.

Port of openclaw `extensions/telegram/src/accounts.ts`.
"""

from __future__ import annotations

from oxenclaw.config.credentials import CredentialStore
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.extensions.telegram.channel import TelegramChannel
from oxenclaw.extensions.telegram.token import TokenResolver
from oxenclaw.plugin_sdk.config_schema import RootConfig
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("extensions.telegram.accounts")


class TelegramAccountRegistry:
    def __init__(
        self,
        *,
        paths: OxenclawPaths | None = None,
        tokens: TokenResolver | None = None,
    ) -> None:
        resolved_paths = paths or default_paths()
        store = CredentialStore(resolved_paths)
        self._tokens = tokens or TokenResolver(store)
        self._channels: dict[str, TelegramChannel] = {}

    def load_from_config(self, config: RootConfig) -> list[str]:
        """Create channel instances for every account declared under `channels.telegram.accounts`.

        Accounts whose token cannot be resolved are skipped with a warning —
        callers that want strict behaviour should check `missing()` afterward.
        """
        tg_cfg = config.channels.get("telegram")
        loaded: list[str] = []
        if tg_cfg is None:
            return loaded
        for acct in tg_cfg.accounts:
            account_id = acct.account_id
            if account_id in self._channels:
                continue
            token = self._tokens.resolve(account_id)
            if token is None:
                logger.warning("telegram account %r has no token; skipping", account_id)
                continue
            self._channels[account_id] = TelegramChannel(token=token, account_id=account_id)
            loaded.append(account_id)
        return loaded

    def register(self, channel: TelegramChannel) -> None:
        if channel.id != "telegram":
            raise ValueError(f"expected telegram channel, got {channel.id!r}")
        account = channel._account_id
        if account in self._channels:
            raise ValueError(f"account {account!r} already registered")
        self._channels[account] = channel

    def get(self, account_id: str) -> TelegramChannel | None:
        return self._channels.get(account_id)

    def require(self, account_id: str) -> TelegramChannel:
        ch = self._channels.get(account_id)
        if ch is None:
            raise UserVisibleError(f"telegram account {account_id!r} not registered")
        return ch

    def ids(self) -> list[str]:
        return sorted(self._channels)

    def all(self) -> list[TelegramChannel]:
        return list(self._channels.values())

    def missing(self, config: RootConfig) -> list[str]:
        """Return account ids declared in config but not loaded (missing tokens, etc.)."""
        tg_cfg = config.channels.get("telegram")
        if tg_cfg is None:
            return []
        declared = {a.account_id for a in tg_cfg.accounts}
        return sorted(declared - set(self._channels))

    async def aclose(self) -> None:
        for channel in self._channels.values():
            await channel.aclose()
        self._channels.clear()
