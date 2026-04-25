"""Telegram channel plugin — wires bot_core + monitor + send into the ChannelPlugin contract.

Port of openclaw `extensions/telegram/src/channel.ts` (text-only B.5a subset).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sampyclaw.extensions.telegram.bot_core import (
    TELEGRAM_CHANNEL_ID,
    create_bot,
)
from sampyclaw.extensions.telegram.monitor import TelegramPollingSession
from sampyclaw.extensions.telegram.send import send_message_telegram
from sampyclaw.plugin_sdk.channel_contract import (
    MonitorOpts,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError
from sampyclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

logger = get_logger("extensions.telegram.channel")


class TelegramChannel:
    """ChannelPlugin for Telegram. One instance binds to one account (B.5a).

    Multi-account fan-out lands in B.5c via an account-indexed registry.
    """

    id = TELEGRAM_CHANNEL_ID

    def __init__(self, *, token: str, account_id: str = "main") -> None:
        if not token:
            raise ValueError("token is required")
        self._token = token
        self._account_id = account_id
        self._bot: Bot | None = None
        self._session: TelegramPollingSession | None = None

    def _require_bot(self) -> Bot:
        if self._bot is None:
            self._bot = create_bot(self._token)
        return self._bot

    async def send(self, params: SendParams) -> SendResult:
        if params.target.channel != TELEGRAM_CHANNEL_ID:
            raise UserVisibleError(
                f"send called on telegram channel with target channel={params.target.channel!r}"
            )
        return await send_message_telegram(self._require_bot(), params)

    async def monitor(self, opts: MonitorOpts) -> None:
        if opts.account_id != self._account_id:
            raise UserVisibleError(
                f"monitor account_id {opts.account_id!r} != bound {self._account_id!r}"
            )
        session = TelegramPollingSession(
            bot=self._require_bot(),
            account_id=self._account_id,
            on_inbound=opts.on_inbound,
        )
        self._session = session
        await session.start()

    async def probe(self, opts: ProbeOpts) -> ProbeResult:
        bot = self._require_bot()
        try:
            me = await bot.get_me()
        except Exception as exc:
            return ProbeResult(ok=False, account_id=opts.account_id, error=str(exc))
        display = me.username or me.full_name
        return ProbeResult(ok=True, account_id=opts.account_id, display_name=display)

    async def aclose(self) -> None:
        """Release the aiogram session. Call on shutdown."""
        if self._session is not None:
            await self._session.stop()
            self._session = None
        if self._bot is not None:
            await self._bot.session.close()
            self._bot = None
