"""B.5b tests: inline keyboards + single-item media on send_message_telegram."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sampyclaw.extensions.telegram.send import send_message_telegram
from sampyclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InlineButton,
    MediaItem,
    SendParams,
)


def _params(**overrides):  # type: ignore[no-untyped-def]
    base = dict(
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        text="hi",
    )
    base.update(overrides)
    return SendParams(**base)  # type: ignore[arg-type]


def _sent_message(message_id: int = 1) -> MagicMock:
    m = MagicMock()
    m.message_id = message_id
    m.date = datetime(2026, 4, 24, tzinfo=UTC)
    return m


def _mock_bot_with(method_name: str) -> MagicMock:
    bot = MagicMock()
    setattr(bot, method_name, AsyncMock(return_value=_sent_message()))
    return bot


async def test_inline_keyboard_passed_as_reply_markup() -> None:
    bot = _mock_bot_with("send_message")
    buttons = [
        [InlineButton(text="Yes", callback_data="y"), InlineButton(text="No", callback_data="n")],
        [InlineButton(text="Docs", url="https://example.com")],
    ]
    await send_message_telegram(bot, _params(buttons=buttons))
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    assert markup is not None
    assert len(markup.inline_keyboard) == 2
    assert len(markup.inline_keyboard[0]) == 2
    assert markup.inline_keyboard[0][0].text == "Yes"
    assert markup.inline_keyboard[1][0].url == "https://example.com"


async def test_single_photo_via_file_id() -> None:
    bot = _mock_bot_with("send_photo")
    await send_message_telegram(
        bot,
        _params(
            text="caption here",
            media=[MediaItem(kind="photo", source="AgACAgQAAx_file_id_xxx")],
        ),
    )
    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.call_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["photo"] == "AgACAgQAAx_file_id_xxx"
    assert kwargs["caption"] == "caption here"


async def test_single_photo_via_url() -> None:
    bot = _mock_bot_with("send_photo")
    await send_message_telegram(
        bot,
        _params(
            text=None,
            media=[MediaItem(kind="photo", source="https://example.com/pic.jpg")],
        ),
    )
    from aiogram.types import URLInputFile

    assert isinstance(bot.send_photo.call_args.kwargs["photo"], URLInputFile)
    assert "caption" not in bot.send_photo.call_args.kwargs


async def test_single_document_via_local_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bot = _mock_bot_with("send_document")
    local = tmp_path / "hello.txt"
    local.write_text("hi")
    await send_message_telegram(
        bot,
        _params(
            text="here is the file",
            media=[MediaItem(kind="document", source=str(local))],
        ),
    )
    from aiogram.types import FSInputFile

    assert isinstance(bot.send_document.call_args.kwargs["document"], FSInputFile)
    assert bot.send_document.call_args.kwargs["caption"] == "here is the file"


async def test_sticker_suppresses_caption() -> None:
    bot = _mock_bot_with("send_sticker")
    await send_message_telegram(
        bot,
        _params(
            text="ignored",
            media=[MediaItem(kind="sticker", source="file_id_sticker")],
        ),
    )
    assert "caption" not in bot.send_sticker.call_args.kwargs


async def test_media_item_caption_overrides_params_text() -> None:
    bot = _mock_bot_with("send_photo")
    await send_message_telegram(
        bot,
        _params(
            text="outer",
            media=[MediaItem(kind="photo", source="fid", caption="inner")],
        ),
    )
    assert bot.send_photo.call_args.kwargs["caption"] == "inner"


async def test_multiple_media_items_not_yet_supported() -> None:
    bot = MagicMock()
    params = _params(
        media=[
            MediaItem(kind="photo", source="a"),
            MediaItem(kind="photo", source="b"),
        ]
    )
    with pytest.raises(NotImplementedError):
        await send_message_telegram(bot, params)


async def test_video_kind_dispatches_to_send_video() -> None:
    bot = _mock_bot_with("send_video")
    await send_message_telegram(
        bot, _params(text=None, media=[MediaItem(kind="video", source="fid")])
    )
    bot.send_video.assert_awaited_once()


async def test_voice_and_audio_and_animation_dispatch() -> None:
    for kind, method in [
        ("voice", "send_voice"),
        ("audio", "send_audio"),
        ("animation", "send_animation"),
    ]:
        bot = _mock_bot_with(method)
        await send_message_telegram(
            bot, _params(text=None, media=[MediaItem(kind=kind, source="fid")])
        )
        getattr(bot, method).assert_awaited_once()
