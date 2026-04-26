"""Thin async Slack Web API client over the existing guarded session.

Only `chat.postMessage` is implemented — the outbound-only goal doesn't
need conversations.list, users.info, or anything that drives reactive
behaviour. Adding more methods is one helper each on this class.

Hardening:
- Goes through `security/net/guarded_session()` so SSRF / DNS pinning
  / scheme + port guards apply automatically. Operators talking to a
  corp-internal Slack proxy add the proxy hostname to
  `OXENCLAW_NET_ALLOWED_HOSTNAMES` (or set `NetPolicy.allowed_hostnames`
  programmatically) — there is no per-channel allowlist override here.
- Retries 429 (`Retry-After` honoured), 5xx, and connection drops with
  full-jitter backoff (max 3 retries by default).
- Bot tokens are passed as `Authorization: Bearer <token>` per Slack
  docs; the value is never logged.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import aiohttp

from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.security.net.guarded_fetch import guarded_session
from oxenclaw.security.net.policy import NetPolicy, policy_from_env

logger = get_logger("extensions.slack.client")

DEFAULT_BASE_URL = "https://slack.com/api"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 3


class SlackApiError(RuntimeError):
    """Raised when the Slack API responds with `ok: false`."""

    def __init__(self, error_code: str, *, status: int, response: dict | None = None):
        super().__init__(f"slack api error: {error_code} (http {status})")
        self.error_code = error_code
        self.status = status
        self.response = response or {}


class SlackWebClient:
    """Outbound-only Slack Web API wrapper.

    One client per (account, base_url) — typically one per workspace.
    For Enterprise Grid the same client object talks to every workspace
    the bot is installed in; the `channel` arg on `post_message` is the
    conversation ID that disambiguates.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        policy: NetPolicy | None = None,
    ) -> None:
        if not token:
            raise ValueError("token is required")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._policy = policy or policy_from_env()
        self._session: aiohttp.ClientSession | None = None
        self._session_cm: Any = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            # `guarded_session` is an async context manager; we own the
            # entry/exit so the caller can reuse the connection across
            # many sends without paying TLS handshake every time.
            self._session_cm = guarded_session(self._policy)
            self._session = await self._session_cm.__aenter__()
        return self._session

    async def aclose(self) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(None, None, None)
            self._session_cm = None
            self._session = None

    async def post_message(
        self,
        *,
        channel: str,
        text: str | None = None,
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
        unfurl_links: bool = False,
        unfurl_media: bool = False,
    ) -> dict[str, Any]:
        """`chat.postMessage` — Slack returns `{ok, channel, ts, message}` on success."""
        if not text and not blocks:
            raise ValueError("post_message requires text or blocks")
        payload: dict[str, Any] = {
            "channel": channel,
            "unfurl_links": unfurl_links,
            "unfurl_media": unfurl_media,
        }
        if text is not None:
            payload["text"] = text
        if blocks is not None:
            payload["blocks"] = blocks
        if thread_ts is not None:
            payload["thread_ts"] = thread_ts
        if username is not None:
            payload["username"] = username
        if icon_emoji is not None:
            payload["icon_emoji"] = icon_emoji
        return await self._call("chat.postMessage", payload)

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        attempt = 0
        while True:
            session = await self._ensure_session()
            try:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status in RETRYABLE_STATUS and attempt < self._max_retries:
                        retry_after = float(resp.headers.get("Retry-After", "0") or 0)
                        body_preview = (await resp.text())[:200]
                        logger.warning(
                            "slack %s retryable %s (attempt %d/%d): %s",
                            method,
                            resp.status,
                            attempt + 1,
                            self._max_retries,
                            body_preview,
                        )
                        await asyncio.sleep(self._delay(attempt, retry_after))
                        attempt += 1
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
            except (TimeoutError, aiohttp.ClientConnectionError) as exc:
                if attempt >= self._max_retries:
                    raise
                logger.warning(
                    "slack %s transient error (attempt %d/%d): %s",
                    method,
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                await asyncio.sleep(self._delay(attempt, 0))
                attempt += 1
                continue
            if not data.get("ok"):
                raise SlackApiError(
                    str(data.get("error") or "unknown"),
                    status=resp.status,
                    response=data,
                )
            return data

    def _delay(self, attempt: int, retry_after: float) -> float:
        # Honour Slack's Retry-After when it tells us; otherwise full
        # jitter with 0.5 → 8s envelope.
        if retry_after > 0:
            return min(retry_after, 30.0)
        base = min(8.0, 0.5 * (2**attempt))
        return random.uniform(0, base)
