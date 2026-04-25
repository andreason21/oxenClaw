"""Telegram bot token resolution.

Lookup order for a given account:
1. `~/.sampyclaw/credentials/telegram/<account_id>.json` (the CredentialStore)
2. `TELEGRAM_BOT_TOKEN` env var (single-bot shortcut, only when the account is `main`)

Port of openclaw `extensions/telegram/src/token.ts`.
"""

from __future__ import annotations

import os

from sampyclaw.config.credentials import CredentialStore
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError

TELEGRAM_CHANNEL = "telegram"
DEFAULT_ENV_KEY = "TELEGRAM_BOT_TOKEN"


class TokenResolver:
    def __init__(
        self, store: CredentialStore, *, env_key: str = DEFAULT_ENV_KEY
    ) -> None:
        self._store = store
        self._env_key = env_key

    def resolve(self, account_id: str) -> str | None:
        cred = self._store.read(TELEGRAM_CHANNEL, account_id)
        if cred is not None:
            token = cred.get("token")
            if isinstance(token, str) and token:
                return token
        if account_id == "main":
            return os.environ.get(self._env_key) or None
        return None

    def require(self, account_id: str) -> str:
        token = self.resolve(account_id)
        if token is None:
            raise UserVisibleError(
                f"no telegram token for account {account_id!r} "
                f"(looked in credentials and ${self._env_key})"
            )
        return token

    def write(self, account_id: str, token: str) -> None:
        if not token:
            raise ValueError("token must be non-empty")
        self._store.write(TELEGRAM_CHANNEL, account_id, {"token": token})

    def rotate(self, account_id: str, new_token: str) -> str | None:
        """Replace the stored token and return the previous value (or None)."""
        existing = self._store.read(TELEGRAM_CHANNEL, account_id)
        previous = existing.get("token") if existing else None
        self.write(account_id, new_token)
        return previous if isinstance(previous, str) else None

    def forget(self, account_id: str) -> bool:
        return self._store.delete(TELEGRAM_CHANNEL, account_id)
