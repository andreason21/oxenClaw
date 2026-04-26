"""Telegram polling monitor.

Port of the polling path of openclaw `extensions/telegram/src/monitor.ts` +
`monitor-polling.runtime.ts`. Webhook transport arrives in B.5c.

aiogram's built-in `Dispatcher.start_polling()` handles `getUpdates` offset
tracking for us; we layer our own `UpdateDeduplicator` on top for symmetry
with openclaw's belt-and-suspenders behaviour across restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxenclaw.extensions.telegram.bot_core import (
    TELEGRAM_CHANNEL_ID,
    UpdateDeduplicator,
    envelope_from_message,
)
from oxenclaw.plugin_sdk.channel_contract import InboundHandler
from oxenclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

logger = get_logger("extensions.telegram.monitor")


class TelegramPollingSession:
    """Wraps an aiogram Dispatcher + polling task for one account."""

    def __init__(
        self,
        *,
        bot: Bot,
        account_id: str,
        on_inbound: InboundHandler,
        dedup: UpdateDeduplicator | None = None,
    ) -> None:
        from aiogram import Dispatcher

        self._bot = bot
        self._account_id = account_id
        self._on_inbound = on_inbound
        self._dedup = dedup or UpdateDeduplicator()
        self._dispatcher = Dispatcher()
        self._dispatcher.message.register(self._handle_message)

    async def _handle_message(self, message: Message) -> None:
        if self._dedup.seen(message.message_id):
            logger.debug("dropping duplicate telegram message_id=%s", message.message_id)
            return
        envelope = await envelope_from_message(message, account_id=self._account_id, bot=self._bot)
        if envelope is None:
            return
        try:
            await self._on_inbound(envelope)
        except Exception:
            logger.exception(
                "inbound handler raised for %s:%s",
                TELEGRAM_CHANNEL_ID,
                envelope.target.chat_id,
            )

    async def start(self) -> None:
        """Block on `getUpdates` polling until stop() is called."""
        logger.info("telegram polling start for account=%s", self._account_id)
        await self._dispatcher.start_polling(self._bot, handle_signals=False)

    async def stop(self) -> None:
        await self._dispatcher.stop_polling()
