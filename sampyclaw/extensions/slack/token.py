"""Slack bot token resolution.

Lookup order for a given account:
1. `~/.sampyclaw/credentials/slack/<account_id>.json` — the same
   CredentialStore Telegram uses, mode 0600.
2. `SLACK_BOT_TOKEN` env var (single-bot shortcut, only when the
   account is `main`).

Workspace bot tokens start with `xoxb-`; Enterprise Grid org-wide
tokens start with `xoxe.xoxb-`. Both authenticate the same
`chat.postMessage` API and we don't gate on the prefix.
"""

from __future__ import annotations

import os

from sampyclaw.config.credentials import CredentialStore
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError

SLACK_CHANNEL = "slack"
DEFAULT_ENV_KEY = "SLACK_BOT_TOKEN"


class SlackTokenResolver:
    def __init__(self, store: CredentialStore, *, env_key: str = DEFAULT_ENV_KEY) -> None:
        self._store = store
        self._env_key = env_key

    def resolve(self, account_id: str) -> str | None:
        cred = self._store.read(SLACK_CHANNEL, account_id)
        if cred is not None:
            token = cred.get("token") or cred.get("bot_token")
            if isinstance(token, str) and token:
                return token
        if account_id == "main":
            return os.environ.get(self._env_key) or None
        return None

    def require(self, account_id: str) -> str:
        token = self.resolve(account_id)
        if token is None:
            raise UserVisibleError(
                f"no slack token for account {account_id!r} "
                f"(looked in credentials and ${self._env_key})"
            )
        return token

    def write(self, account_id: str, token: str) -> None:
        if not token:
            raise ValueError("token must be non-empty")
        self._store.write(SLACK_CHANNEL, account_id, {"token": token})
