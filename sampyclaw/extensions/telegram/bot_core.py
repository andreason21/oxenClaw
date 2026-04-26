"""Telegram bot factory, update dedup, and Message→InboundEnvelope adapter.

Port of the text-only subset of openclaw `extensions/telegram/src/bot-core.ts`
and `bot-message-context.ts`.
"""

from __future__ import annotations

import base64
from collections import deque
from typing import TYPE_CHECKING

from sampyclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InboundEnvelope,
    MediaItem,
)
from sampyclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message, PhotoSize

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


def _largest_photo(photos: list[PhotoSize]) -> PhotoSize | None:
    """Telegram delivers PhotoSize variants; pick the largest by file_size
    (falls back to area if size is missing on older API responses)."""
    if not photos:
        return None

    def _key(p: PhotoSize) -> tuple[int, int]:
        return (
            p.file_size or 0,
            (p.width or 0) * (p.height or 0),
        )

    return max(photos, key=_key)


async def _photo_to_media_item(
    bot: Bot, photo: PhotoSize, *, max_bytes: int = 10 * 1024 * 1024
) -> MediaItem | None:
    """Download one PhotoSize via Bot API and embed as a data: URI.

    Embedding (rather than passing a `t.me/.../file/...` URL) lets the
    rest of the pipeline treat all images uniformly and gives us
    one round-trip through the SSRF guard at fetch time.
    Returns None on download failure — caller logs + drops.
    """
    if photo.file_size and photo.file_size > max_bytes:
        logger.warning(
            "photo %s exceeds max_bytes=%d (size=%d) — dropping",
            photo.file_id,
            max_bytes,
            photo.file_size,
        )
        return None
    try:
        file = await bot.get_file(photo.file_id)
        # aiogram's `download` writes to a stream; we want bytes.
        from io import BytesIO

        buf = BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        raw = buf.getvalue()
    except Exception as exc:
        logger.warning("photo download failed (%s): %s", photo.file_id, exc)
        return None
    if len(raw) > max_bytes:
        logger.warning(
            "photo %s too large after download (%d) — dropping",
            photo.file_id,
            len(raw),
        )
        return None
    # Telegram's photo files are always JPEG — but sniff anyway so a
    # future format swap doesn't silently break.
    if raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        logger.warning("photo %s has unrecognized format — dropping", photo.file_id)
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return MediaItem(
        kind="photo",
        source=f"data:{mime};base64,{b64}",
        mime_type=mime,
    )


async def envelope_from_message(
    message: Message,
    *,
    account_id: str,
    bot: Bot | None = None,
) -> InboundEnvelope | None:
    """Translate an aiogram Message into our canonical InboundEnvelope.

    Returns None when the message has no consumable content (no text,
    no caption, no photo).

    `bot` is required to download photos; if omitted, photos are
    silently skipped (text-only mode kept for legacy callers and tests
    that don't exercise media).
    """
    text = message.text or message.caption

    chat = message.chat
    if chat is None:
        logger.debug("ignoring message %s without chat", message.message_id)
        return None

    media_items: list[MediaItem] = []
    if bot is not None and message.photo:
        largest = _largest_photo(list(message.photo))
        if largest is not None:
            item = await _photo_to_media_item(bot, largest)
            if item is not None:
                media_items.append(item)

    if text is None and not media_items:
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
        media=media_items,
        received_at=received_at,
    )
