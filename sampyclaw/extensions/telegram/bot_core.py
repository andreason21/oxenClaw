"""Telegram bot factory, update dedup, and Message→InboundEnvelope adapter.

Port of the text-only subset of openclaw `extensions/telegram/src/bot-core.ts`
and `bot-message-context.ts`.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope
from sampyclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

logger = get_logger("extensions.telegram.bot_core")

TELEGRAM_CHANNEL_ID = "telegram"


class UpdateDeduplicator:
    """Bounded set + FIFO eviction for update_id dedup across polling retries.

    openclaw persists dedup state to survive restarts; B.5a keeps it in-memory only.
    """

    def __init__(self, capacity: int = 2048) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._seen: set[int] = set()
        self._order: deque[int] = deque()
        self._capacity = capacity

    def seen(self, update_id: int) -> bool:
        """Return True if already seen. Otherwise record and return False."""
        if update_id in self._seen:
            return True
        self._seen.add(update_id)
        self._order.append(update_id)
        if len(self._order) > self._capacity:
            evicted = self._order.popleft()
            self._seen.discard(evicted)
        return False

    def __len__(self) -> int:
        return len(self._order)


def create_bot(token: str) -> Bot:
    """Create an aiogram Bot. Imported lazily so the SDK stays importable without aiogram."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))


def envelope_from_message(
    message: Message, *, account_id: str
) -> InboundEnvelope | None:
    """Translate an aiogram Message into our canonical InboundEnvelope.

    Text-only for B.5a: returns None if the message has neither `text` nor `caption`.
    Media extraction, entities, and reply threading land in B.5b.
    """
    text = message.text or message.caption
    if text is None:
        return None

    chat = message.chat
    if chat is None:
        logger.debug("ignoring message %s without chat", message.message_id)
        return None

    thread_id = None
    if message.message_thread_id is not None:
        thread_id = str(message.message_thread_id)

    target = ChannelTarget(
        channel=TELEGRAM_CHANNEL_ID,
        account_id=account_id,
        chat_id=str(chat.id),
        thread_id=thread_id,
    )

    sender = message.from_user
    sender_id = str(sender.id) if sender else "anonymous"
    sender_name = sender.full_name if sender else None
    received_at = message.date.timestamp() if message.date else 0.0

    return InboundEnvelope(
        channel=TELEGRAM_CHANNEL_ID,
        account_id=account_id,
        target=target,
        sender_id=sender_id,
        sender_display_name=sender_name,
        text=text,
        received_at=received_at,
    )
