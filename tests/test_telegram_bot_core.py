"""Tests for Telegram bot_core: update dedup and Message→InboundEnvelope translation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sampyclaw.extensions.telegram.bot_core import (
    UpdateDeduplicator,
    envelope_from_message,
)


def _make_message(
    *,
    text: str | None = "hello",
    caption: str | None = None,
    message_id: int = 1,
    chat_id: int = 42,
    user_id: int | None = 100,
    thread_id: int | None = None,
):  # type: ignore[no-untyped-def]
    from aiogram.types import Chat, Message, User

    user = None
    if user_id is not None:
        user = User(id=user_id, is_bot=False, first_name="Alice", last_name="Wonder")
    return Message.model_validate(
        {
            "message_id": message_id,
            "date": int(datetime(2026, 4, 24, tzinfo=UTC).timestamp()),
            "chat": Chat(id=chat_id, type="private").model_dump(),
            "from": user.model_dump() if user else None,
            "text": text,
            "caption": caption,
            "message_thread_id": thread_id,
        }
    )


def test_dedup_first_seen_is_false_second_true() -> None:
    d = UpdateDeduplicator()
    assert d.seen(1) is False
    assert d.seen(1) is True
    assert d.seen(2) is False


def test_dedup_evicts_oldest_past_capacity() -> None:
    d = UpdateDeduplicator(capacity=3)
    for i in range(3):
        d.seen(i)
    assert len(d) == 3
    d.seen(99)  # triggers eviction of 0
    assert len(d) == 3
    assert d.seen(0) is False  # was evicted, so now unseen again
    assert d.seen(99) is True


def test_dedup_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError):
        UpdateDeduplicator(capacity=0)


async def test_envelope_from_text_message() -> None:
    env = await envelope_from_message(_make_message(text="hi there"), account_id="main")
    assert env is not None
    assert env.channel == "telegram"
    assert env.account_id == "main"
    assert env.text == "hi there"
    assert env.target.chat_id == "42"
    assert env.target.thread_id is None
    assert env.sender_id == "100"
    assert env.sender_display_name == "Alice Wonder"


async def test_envelope_from_caption_only() -> None:
    env = await envelope_from_message(
        _make_message(text=None, caption="photo caption"), account_id="main"
    )
    assert env is not None
    assert env.text == "photo caption"


async def test_envelope_without_text_returns_none() -> None:
    env = await envelope_from_message(_make_message(text=None, caption=None), account_id="main")
    assert env is None


async def test_envelope_preserves_thread_id_as_string() -> None:
    env = await envelope_from_message(_make_message(thread_id=777), account_id="main")
    assert env is not None
    assert env.target.thread_id == "777"


async def test_envelope_anonymous_when_no_sender() -> None:
    env = await envelope_from_message(_make_message(user_id=None), account_id="main")
    assert env is not None
    assert env.sender_id == "anonymous"
    assert env.sender_display_name is None


# ─── photo / multimodal coverage ─────────────────────────────────────


_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01" + b"x" * 64
_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _make_message_with_photo(
    *,
    caption: str | None = None,
    photo_size: int | None = 1024,
):  # type: ignore[no-untyped-def]
    from aiogram.types import Chat, Message, User

    user = User(id=100, is_bot=False, first_name="Alice")
    return Message.model_validate(
        {
            "message_id": 1,
            "date": int(datetime(2026, 4, 24, tzinfo=UTC).timestamp()),
            "chat": Chat(id=42, type="private").model_dump(),
            "from": user.model_dump(),
            "caption": caption,
            "photo": [
                {
                    "file_id": "small",
                    "file_unique_id": "u1",
                    "width": 90,
                    "height": 90,
                    "file_size": 100,
                },
                {
                    "file_id": "large",
                    "file_unique_id": "u2",
                    "width": 1280,
                    "height": 720,
                    "file_size": photo_size,
                },
            ],
        }
    )


class _FakeBot:
    """Minimal aiogram Bot stand-in: get_file + download_file."""

    def __init__(self, payload: bytes, *, file_path: str = "photos/file.jpg") -> None:
        self.payload = payload
        self.file_path = file_path
        self.downloaded_paths: list[str] = []

    async def get_file(self, file_id: str):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        return SimpleNamespace(file_path=self.file_path, file_id=file_id)

    async def download_file(self, file_path: str, *, destination) -> None:  # type: ignore[no-untyped-def]
        self.downloaded_paths.append(file_path)
        destination.write(self.payload)


async def test_envelope_from_photo_only_message_attaches_media() -> None:
    bot = _FakeBot(_JPEG_HEADER)
    env = await envelope_from_message(
        _make_message_with_photo(caption=None),
        account_id="main",
        bot=bot,  # type: ignore[arg-type]
    )
    assert env is not None
    assert env.text is None
    assert len(env.media) == 1
    item = env.media[0]
    assert item.kind == "photo"
    assert item.mime_type == "image/jpeg"
    assert item.source.startswith("data:image/jpeg;base64,")
    # Exactly the largest photo was downloaded.
    assert bot.downloaded_paths == ["photos/file.jpg"]


async def test_envelope_from_photo_with_caption_keeps_both() -> None:
    bot = _FakeBot(_PNG_HEADER)
    env = await envelope_from_message(
        _make_message_with_photo(caption="check this out"),
        account_id="main",
        bot=bot,  # type: ignore[arg-type]
    )
    assert env is not None
    assert env.text == "check this out"
    assert len(env.media) == 1
    assert env.media[0].mime_type == "image/png"


async def test_envelope_drops_oversized_photo_silently() -> None:
    bot = _FakeBot(_JPEG_HEADER)
    msg = _make_message_with_photo(
        photo_size=50 * 1024 * 1024  # 50 MB — over the 10 MiB cap
    )
    env = await envelope_from_message(
        msg,
        account_id="main",
        bot=bot,  # type: ignore[arg-type]
    )
    # No text, no usable photo → envelope is None (no consumable content).
    assert env is None
    # Bot was NOT asked to download the file.
    assert bot.downloaded_paths == []


async def test_envelope_drops_unrecognized_format() -> None:
    bot = _FakeBot(b"GARBAGE-no-magic-bytes")
    env = await envelope_from_message(
        _make_message_with_photo(caption="cap"),
        account_id="main",
        bot=bot,  # type: ignore[arg-type]
    )
    # Caption preserves the message, but the photo is dropped.
    assert env is not None
    assert env.text == "cap"
    assert env.media == []


async def test_envelope_skips_photos_when_bot_omitted() -> None:
    """Legacy callers passing no `bot` get text-only behaviour back."""
    env = await envelope_from_message(_make_message_with_photo(caption="hi"), account_id="main")
    assert env is not None
    assert env.text == "hi"
    assert env.media == []
