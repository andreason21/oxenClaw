"""Telegram outbound send.

Port of openclaw `extensions/telegram/src/send.ts`. B.5b adds single-item media
(photo/video/audio/voice/document/sticker/animation) and inline keyboards.
Media groups (`send_media_group`, multiple items) and streaming edits remain
deferred to B.5c+.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sampyclaw.plugin_sdk.channel_contract import (
    InlineButton,
    MediaItem,
    SendParams,
    SendResult,
)
from sampyclaw.plugin_sdk.error_runtime import (
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)

if TYPE_CHECKING:
    from aiogram import Bot


# Maps our MediaItem.kind → (bot method name, keyword arg for the payload).
_MEDIA_METHODS: dict[str, tuple[str, str]] = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "audio": ("send_audio", "audio"),
    "voice": ("send_voice", "voice"),
    "document": ("send_document", "document"),
    "sticker": ("send_sticker", "sticker"),
    "animation": ("send_animation", "animation"),
}

# Telegram rejects captions on stickers; they'd throw a BadRequest if we passed one.
_NO_CAPTION_KINDS: frozenset[str] = frozenset({"sticker"})


async def send_message_telegram(bot: Bot, params: SendParams) -> SendResult:
    """Dispatch to the right aiogram Bot method based on params shape.

    - no media + text -> send_message
    - exactly one media item -> send_photo / send_video / etc., with `text` as caption
    - many media items -> NotImplementedError (B.5c)
    """
    if len(params.media) > 1:
        raise NotImplementedError("media groups arrive in B.5c")

    reply_markup = _to_reply_markup(params.buttons)
    chat_id = int(params.target.chat_id)
    thread_id = (
        int(params.target.thread_id) if params.target.thread_id is not None else None
    )
    reply_to = (
        int(params.reply_to_message_id)
        if params.reply_to_message_id is not None
        else None
    )

    try:
        if not params.media:
            if not params.text:
                raise ValueError("send requires text when no media is attached")
            msg = await bot.send_message(
                chat_id=chat_id,
                text=params.text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
                reply_markup=reply_markup,
            )
        else:
            msg = await _send_single_media(
                bot,
                chat_id=chat_id,
                item=params.media[0],
                caption=params.text,
                thread_id=thread_id,
                reply_to=reply_to,
                reply_markup=reply_markup,
            )
    except Exception as exc:
        _reraise_as_sdk_error(exc)

    return SendResult(
        message_id=str(msg.message_id),
        timestamp=msg.date.timestamp() if msg.date else 0.0,
    )


async def _send_single_media(
    bot: Bot,
    *,
    chat_id: int,
    item: MediaItem,
    caption: str | None,
    thread_id: int | None,
    reply_to: int | None,
    reply_markup: Any,
) -> Any:
    method_name, payload_kwarg = _MEDIA_METHODS.get(item.kind, (None, None))  # type: ignore[assignment]
    if method_name is None or payload_kwarg is None:
        raise ValueError(f"unsupported media kind: {item.kind}")

    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        payload_kwarg: _resolve_input(item.source),
        "message_thread_id": thread_id,
        "reply_to_message_id": reply_to,
        "reply_markup": reply_markup,
    }

    effective_caption = item.caption if item.caption is not None else caption
    if effective_caption and item.kind not in _NO_CAPTION_KINDS:
        kwargs["caption"] = effective_caption

    method = getattr(bot, method_name)
    return await method(**kwargs)


def _resolve_input(source: str) -> Any:
    """Accept URLs, local file paths, and raw Telegram file_ids.

    - `http(s)://` → URLInputFile (Telegram fetches)
    - existing local path → FSInputFile (multipart upload)
    - anything else → bare string, treated as file_id
    """
    from aiogram.types import FSInputFile, URLInputFile

    if source.startswith(("http://", "https://")):
        return URLInputFile(source)
    if Path(source).is_file():
        return FSInputFile(source)
    return source


def _to_reply_markup(buttons: list[list[InlineButton]]) -> Any:
    if not buttons:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [
            InlineKeyboardButton(
                text=b.text,
                callback_data=b.callback_data,
                url=b.url,
            )
            for b in row
        ]
        for row in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _reraise_as_sdk_error(exc: BaseException) -> None:
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramNetworkError,
        TelegramRetryAfter,
    )

    if isinstance(exc, TelegramRetryAfter):
        raise RateLimitedError(str(exc), retry_after=float(exc.retry_after)) from exc
    if isinstance(exc, TelegramNetworkError):
        raise NetworkError(str(exc)) from exc
    if isinstance(exc, TelegramBadRequest):
        raise UserVisibleError(str(exc)) from exc
    raise exc
